"""The request object is built only for an action that can read it."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.http import Stream
from hinoki.http.request import Request
from hinoki.renderers import JSONRenderer
from hinoki.runtime import requests as request_module
from hinoki.testing import ASGIClient
from tests.harness import Client, Pool


class Note(Schema):
    text: str


@http.get("/plain/{name}")
async def plain(name: str) -> dict[str, str]:
    return {"name": name}


@http.get("/reads")
async def reads(request: Request) -> dict[str, str]:
    return {"method": request.method}


@http.get("/pretty", renderers=[JSONRenderer(indent=2)])
async def pretty() -> dict[str, int]:
    return {"a": 1}


@http.post("/notes", status=201)
async def make(data: Note) -> dict[str, str]:
    return {"text": data.text}


async def chunks() -> AsyncIterator[str]:
    yield "a;"
    yield "b;"


@http.get("/stream")
async def streams() -> Stream:
    return Stream(chunks(), content_type="text/plain")


@http.get("/stream-reads")
async def streams_reading(request: Request) -> Stream:
    return Stream(chunks(), content_type=request.method.lower())


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[Request]:
    made: list[Request] = []

    def counting(*args: Any, **kwargs: Any) -> Request:
        request = Request(*args, **kwargs)
        made.append(request)
        return request

    monkeypatch.setattr(request_module, "Request", counting)
    return made


async def serve(pool: Pool) -> Client:
    app = Hinoki()
    app.include(plain, reads, pretty, make)
    return await pool.start(app)


async def test_an_action_that_never_reads_the_request_builds_none(
    pool: Pool, built: list[Request]
) -> None:
    client = await serve(pool)
    reply = await client.request("GET", "/plain/a")
    assert (reply.status, reply.body) == (200, {"name": "a"})
    assert built == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/reads", b""),
        ("GET", "/pretty", b""),
        ("POST", "/notes", json.dumps({"text": "hi"}).encode()),
    ],
)
async def test_an_action_that_may_read_it_gets_one(
    pool: Pool, built: list[Request], method: str, path: str, body: bytes
) -> None:
    client = await serve(pool)
    reply = await client.request(
        method, path, body=body, content_type="application/json" if body else None
    )
    assert reply.status in {200, 201}
    assert len(built) == 1


async def test_a_streaming_action_builds_a_request_only_when_it_reads_one(
    built: list[Request],
) -> None:
    app = Hinoki()
    app.include(streams, streams_reading)
    async with ASGIClient(app) as client:
        plain_reply = await client.get("/stream")
        assert (plain_reply.status, plain_reply.body) == (200, "a;b;")
        assert built == []

        reading = await client.get("/stream-reads")
        assert reading.status == 200
        assert reading.headers["content-type"] == "get"
        assert len(built) == 1
