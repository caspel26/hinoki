"""``Enum`` and ``Literal`` fields through every request source and both
request lanes: JSON bodies, forms, query strings and XML, on the ASGI lane and
on the Rust server's native lane."""

from __future__ import annotations

import enum
from typing import Any, Literal

from hinoki import XML, Field, Form, Hinoki, Query, Schema, XMLParser, http
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.rustservertest import JSON, fetch
from tests.xmlnativelanetest import recording

FORM = "application/x-www-form-urlencoded"


class Colour(enum.StrEnum):
    RED = "red"
    GREEN = "green"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Tone(enum.Enum):
    WARM = "warm"
    COLD = "cold"


class Pick(Schema):
    colour: Colour
    level: Level = Level.LOW
    tone: Tone = Tone.WARM
    mode: Literal["a", "b"] = "a"
    size: Literal[1, 2] = 1
    colours: list[Colour] = Field(default_factory=list)


def shown(pick: Pick) -> dict[str, Any]:
    return {
        "colour": type(pick.colour).__name__ + ":" + pick.colour.value,
        "level": type(pick.level).__name__ + ":" + str(pick.level.value),
        "tone": type(pick.tone).__name__ + ":" + pick.tone.value,
        "mode": pick.mode,
        "size": pick.size,
        "colours": [c.value for c in pick.colours],
    }


@http.post("/enumlanes/json")
async def json_pick(data: Pick) -> Pick:
    return data


@http.post("/enumlanes/form")
def form_pick(data: Form[Pick]) -> dict[str, Any]:
    return shown(data)


@http.get("/enumlanes/query")
def query_pick(filters: Query[Pick]) -> dict[str, Any]:
    return shown(filters)


@http.post("/enumlanes/xml", parsers=[XMLParser()])
def xml_pick(data: XML[Pick]) -> dict[str, Any]:
    return shown(data)


EXPECTED = {
    "colour": "Colour:green",
    "level": "Level:3",
    "tone": "Tone:cold",
    "mode": "b",
    "size": 2,
    "colours": ["red", "green"],
}


async def test_forms_read_values() -> None:
    app = Hinoki(include=[form_pick])
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/enumlanes/form",
            body=b"colour=green&level=3&tone=cold&mode=b&size=2"
            b"&colours=red&colours=green",
            headers={"content-type": FORM},
        )
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED
        reply = await client.post(
            "/enumlanes/form",
            body=b"colour=blue&level=2&tone=x&mode=c&size=3&colours=red&colours=pink",
            headers={"content-type": FORM},
        )
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.colour", "one_of"),
            ("data.level", "one_of"),
            ("data.tone", "one_of"),
            ("data.mode", "one_of"),
            ("data.size", "one_of"),
            ("data.colours[1]", "one_of"),
        ]


async def test_query_strings_read_values() -> None:
    app = Hinoki(include=[query_pick])
    async with ASGIClient(app) as client:
        reply = await client.get(
            "/enumlanes/query",
            query="colour=green&level=3&tone=cold&mode=b&size=2"
            "&colours=red&colours=green",
        )
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED
        reply = await client.get("/enumlanes/query", query="colour=green&level=two")
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("filters.level", "type")
        ]


async def test_xml_reads_values() -> None:
    app = Hinoki(include=[xml_pick])
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/enumlanes/xml",
            body=b"<pick><colour>green</colour><level>3</level><tone>cold</tone>"
            b"<mode>b</mode><size>2</size><colours>red</colours>"
            b"<colours>green</colours></pick>",
            headers={"content-type": "application/xml"},
        )
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED
        reply = await client.post(
            "/enumlanes/xml",
            body=b"<pick><colour>blue</colour></pick>",
            headers={"content-type": "application/xml"},
        )
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.colour", "one_of")
        ]


BODIES = [
    b'{"colour": "green", "level": 3, "tone": "cold", "mode": "b", "size": 2,'
    b' "colours": ["red"]}',
    b'{"colour": "red"}',
    b'{"colour": "blue", "level": "3", "tone": 1, "mode": "c", "size": true,'
    b' "colours": ["x", 5]}',
    b'{"colour": null, "level": 2, "tone": "WARM"}',
]


async def test_the_native_lane_validates_and_encodes_like_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_pick, query_pick)
    worker = RustWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    query = "/enumlanes/query?colour=red&level=1&tone=warm"
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/enumlanes/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
        queried = await fetch(port, "GET", query)
    finally:
        await worker.stop()
    assert lanes == ["native"] * (len(BODIES) + 1)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, native, strict=True):
            expected = await client.post(
                "/enumlanes/json",
                body=body,
                headers={"content-type": "application/json"},
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
        path, _, raw_query = query.partition("?")
        expected = await client.get(path, query=raw_query)
        assert queried.status == expected.status == 200
        assert queried.json() == expected.body
    assert native[0].body == (
        b'{"colour":"green","level":3,"tone":"cold","mode":"b","size":2,'
        b'"colours":["red"]}'
    )
    assert native[1].json() == {
        "colour": "red",
        "level": 1,
        "tone": "warm",
        "mode": "a",
        "size": 1,
        "colours": [],
    }
    assert [d["code"] for d in native[2].json()["details"]] == [
        "one_of",
        "type",
        "type",
        "one_of",
        "type",
        "one_of",
        "type",
    ]
    assert [d["code"] for d in native[3].json()["details"]] == [
        "null",
        "one_of",
        "one_of",
    ]
