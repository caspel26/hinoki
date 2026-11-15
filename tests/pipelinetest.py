"""The shared request pipeline, driven without the ASGI protocol."""

import json
from typing import Any

from hinoki import Hinoki, Request, http
from hinoki.runtime.pipeline import Pipeline


class FakeBody:
    """A body source that is not ASGI; it records the limit it was given."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.limits: list[int] = []

    async def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return self.body


@http.get("/ping")
async def ping() -> dict[str, bool]:
    return {"ok": True}


@http.post("/echo")
async def echo(request: Request) -> dict[str, object]:
    return {"got": request.data}


def scope(
    method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None
) -> Any:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "client": ("203.0.113.9", 5000),
        "server": ("example.test", 80),
    }


def pipeline() -> Pipeline:
    app = Hinoki()
    app.include(ping, echo)
    built = Pipeline(app)
    built.lifespan._started = True  # serve without the lifespan protocol
    return built


def header_map(headers: list[tuple[bytes, bytes]]) -> dict[bytes, bytes]:
    return dict(headers)


async def test_the_answer_carries_its_final_headers_and_body() -> None:
    source = FakeBody(b"")
    response = await pipeline().handle(scope("GET", "/ping"), source)
    headers = header_map(response.headers)
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True}
    assert headers[b"content-length"] == str(len(response.body)).encode()
    assert len(headers[b"x-request-id"]) == 32
    assert source.limits == []  # a GET never asks for the body


async def test_a_body_is_read_from_the_source_with_the_configured_limit() -> None:
    built = pipeline()
    source = FakeBody(b'{"a": 1}')
    headers = [(b"content-type", b"application/json")]
    response = await built.handle(scope("POST", "/echo", headers), source)
    assert response.status == 200
    assert json.loads(response.body) == {"got": {"a": 1}}
    assert source.limits == [built.app.runtime.max_body_bytes]


async def test_a_declared_length_over_the_limit_is_refused_before_reading() -> None:
    built = pipeline()
    too_big = str(built.app.runtime.max_body_bytes + 1).encode()
    source = FakeBody(b"")
    headers = [(b"content-length", too_big)]
    response = await built.handle(scope("POST", "/echo", headers), source)
    assert response.status == 413
    assert source.limits == []


async def test_a_new_request_id_is_made_for_every_answer() -> None:
    built = pipeline()
    first = await built.handle(scope("GET", "/ping"), FakeBody(b""))
    second = await built.handle(scope("GET", "/ping"), FakeBody(b""))
    assert (
        header_map(first.headers)[b"x-request-id"]
        != header_map(second.headers)[b"x-request-id"]
    )


async def test_an_unknown_path_is_answered_with_the_policy_headers() -> None:
    response = await pipeline().handle(scope("GET", "/missing"), FakeBody(b""))
    assert response.status == 404
    assert b"x-content-type-options" in header_map(response.headers)
