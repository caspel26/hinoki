"""Plain unions and ``Any`` fields through the request sources and both request
lanes: JSON bodies on the ASGI lane and on the Rust server's native lane, and
the text sources (forms, query strings), which hand a union its text as sent."""

from __future__ import annotations

from typing import Any

from hinoki import Field, Form, Hinoki, Query, Schema, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import JSON, fetch
from tests.xmlnativelanetest import recording

FORM = "application/x-www-form-urlencoded"


class Ticket(Schema):
    code: int | str
    sizes: list[float | str] = Field(default_factory=list)
    extra: Any = None


@http.post("/unionlanes/json")
async def json_ticket(data: Ticket) -> Ticket:
    return data


@http.post("/unionlanes/form")
def form_ticket(data: Form[Ticket]) -> dict[str, Any]:
    return {"code": data.code, "extra": data.extra}


@http.get("/unionlanes/query")
def query_ticket(filters: Query[Ticket]) -> dict[str, Any]:
    return {"code": filters.code, "sizes": filters.sizes}


async def test_text_sources_read_the_first_variant_that_parses() -> None:
    app = Hinoki(include=[form_ticket, query_ticket])
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/unionlanes/form",
            body=b"code=5&extra=x",
            headers={"content-type": FORM},
        )
        assert reply.status == 200, reply.body
        assert reply.body == {"code": 5, "extra": "x"}
        reply = await client.get("/unionlanes/query", query="code=abc&sizes=1.5")
        assert reply.status == 200, reply.body
        assert reply.body == {"code": "abc", "sizes": [1.5]}


BODIES = [
    b'{"code": 7, "sizes": [1, 2.5, "big"], "extra": {"k": [1, null]}}',
    b'{"code": "seven"}',
    b'{"code": 7.5}',
    b'{"code": null}',
    b'{"code": 1, "sizes": [null, true]}',
    b"{}",
]


async def test_the_native_lane_validates_and_encodes_like_the_python_lane() -> None:
    app = Hinoki()
    app.include(json_ticket)
    worker = KisoWorker(app, "127.0.0.1", 0)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle = recording(lanes, "python", pipeline.handle)  # type: ignore[method-assign]
    port = await worker.start()
    try:
        native = [
            await fetch(
                port,
                "POST",
                "/unionlanes/json",
                JSON + f"content-length: {len(body)}\r\n",
                body=body,
            )
            for body in BODIES
        ]
    finally:
        await worker.stop()
    assert lanes == ["native"] * len(BODIES)
    async with ASGIClient(app) as client:
        for body, got in zip(BODIES, native, strict=True):
            expected = await client.post(
                "/unionlanes/json",
                body=body,
                headers={"content-type": "application/json"},
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
    assert native[0].json() == {
        "code": 7,
        "sizes": [1.0, 2.5, "big"],
        "extra": {"k": [1, None]},
    }
