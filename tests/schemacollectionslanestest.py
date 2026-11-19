"""``set``, ``frozenset`` and ``tuple`` fields through every request source and
both request lanes: JSON bodies, forms, query strings and XML, on the ASGI lane
and on the Rust server's native lane."""

from __future__ import annotations

from typing import Any

from hinoki import XML, Field, Form, Hinoki, Query, Schema, XMLParser, http
from hinoki.runtime.rustserver import RustWorker
from hinoki.testing import ASGIClient
from tests.rustservertest import JSON, fetch
from tests.xmlnativelanetest import recording

FORM = "application/x-www-form-urlencoded"


class Basket(Schema):
    tags: set[str] = Field(default_factory=set)
    ids: tuple[int, ...] = ()
    pair: tuple[int, bool] = (0, False)
    seen: frozenset[int] = frozenset()


def shown(basket: Basket) -> dict[str, Any]:
    return {
        "tags": sorted(basket.tags),
        "ids": list(basket.ids),
        "pair": list(basket.pair),
        "seen": sorted(basket.seen),
        "types": [type(getattr(basket, name)).__name__ for name in Basket.specs()],
    }


@http.post("/collectionlanes/json")
async def json_basket(data: Basket) -> Basket:
    return data


@http.post("/collectionlanes/form")
def form_basket(data: Form[Basket]) -> dict[str, Any]:
    return shown(data)


@http.get("/collectionlanes/query")
def query_basket(filters: Query[Basket]) -> dict[str, Any]:
    return shown(filters)


@http.post("/collectionlanes/xml", parsers=[XMLParser()])
def xml_basket(data: XML[Basket]) -> dict[str, Any]:
    return shown(data)


EXPECTED = {
    "tags": ["a", "b"],
    "ids": [3, 1, 3],
    "pair": [2, True],
    "seen": [1, 2],
    "types": ["set", "tuple", "tuple", "frozenset"],
}
PAIRS = "tags=b&tags=a&tags=b&ids=3&ids=1&ids=3&pair=2&pair=true&seen=2&seen=1"


async def test_forms_read_containers() -> None:
    app = Hinoki(include=[form_basket])
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/collectionlanes/form", body=PAIRS.encode(), headers={"content-type": FORM}
        )
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED
        reply = await client.post(
            "/collectionlanes/form",
            body=b"ids=1&ids=x&pair=2&seen=1.5",
            headers={"content-type": FORM},
        )
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.ids[1]", "type"),
            ("data.pair", "min_length"),
            ("data.seen[0]", "type"),
        ]


async def test_query_strings_read_containers() -> None:
    app = Hinoki(include=[query_basket])
    async with ASGIClient(app) as client:
        reply = await client.get("/collectionlanes/query", query=PAIRS)
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED
        reply = await client.get(
            "/collectionlanes/query", query="pair=1&pair=true&pair=3"
        )
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("filters.pair", "max_length")
        ]
        reply = await client.get("/collectionlanes/query", query="tags=only")
        assert reply.body["tags"] == ["only"]


async def test_xml_reads_containers() -> None:
    app = Hinoki(include=[xml_basket])
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/collectionlanes/xml",
            body=b"<basket><tags>b</tags><tags>a</tags><ids>3</ids><ids>1</ids>"
            b"<ids>3</ids><pair>2</pair><pair>true</pair><seen>2</seen>"
            b"<seen>1</seen></basket>",
            headers={"content-type": "application/xml"},
        )
        assert reply.status == 200, reply.body
        assert reply.body == EXPECTED


BODIES = [
    b'{"tags": ["b", "a", "b"], "ids": [3, 1, 3], "pair": [2, true], "seen": [2, 1]}',
    b"{}",
    b'{"tags": "a", "ids": [1, "2"], "pair": [1], "seen": [1.5]}',
    b'{"tags": [1], "ids": null, "pair": [1, true, 3], "seen": {"a": 1}}',
    b'{"pair": ["1", 1]}',
]


async def test_the_native_lane_validates_and_encodes_like_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_basket, query_basket)
    worker = RustWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    query = "/collectionlanes/query?" + PAIRS
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/collectionlanes/json",
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
                "/collectionlanes/json",
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
    assert native[0].json() == {
        "tags": ["a", "b"],
        "ids": [3, 1, 3],
        "pair": [2, True],
        "seen": [1, 2],
    }
