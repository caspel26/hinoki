"""The request object is built only for an action that can read it."""

import json
from typing import Any

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.http.request import Request
from hinoki.renderers import JSONRenderer
from hinoki.runtime import requests as request_module
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
