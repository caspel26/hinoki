"""The shared request pipeline, driven without the ASGI protocol."""

import json
from typing import Any

import pytest

from hinoki import Hinoki, Request, http
from hinoki.runtime.executor import ExecutorSaturated
from hinoki.runtime.pipeline import Pipeline, native_input_exception
from hinoki.runtime.responses import Response


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


def test_unknown_native_input_errors_are_mapped_to_runtime_errors() -> None:
    error = native_input_exception(("unexpected", "native failure", None, []))
    assert isinstance(error, RuntimeError)
    assert str(error) == "native failure"


async def test_native_executor_saturation_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = pipeline()

    async def saturated(_binding: object, _args: object) -> Response:
        raise ExecutorSaturated

    monkeypatch.setattr(built, "_call_native", saturated)
    response = await built._respond_native(built.bindings[0], "r" * 32, {}, None)
    assert response.status == 503


async def test_internal_timeout_exception_is_translated_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = pipeline()

    async def timed_out(_scope: Any, _source: Any, _route: Any) -> Response:
        raise TimeoutError("handler timeout")

    monkeypatch.setattr(built, "_serve", timed_out)
    response = await built._respond(
        scope("GET", "/ping"), FakeBody(b""), "r" * 32, None
    )
    assert response.status == 500


async def test_native_handler_without_a_body_returns_its_declared_status() -> None:
    @http.post("/empty", status=201)
    async def empty() -> None:
        return None

    app = Hinoki()
    app.include(empty)
    built = Pipeline(app)
    binding = built.bindings[0]
    response = await built._call_native(binding, {})
    assert response.status == 201


async def test_native_request_handler_without_request_fails_clearly() -> None:
    built = pipeline()
    binding = built.bindings[1]
    with pytest.raises(
        RuntimeError, match="a handler requiring Request has no request"
    ):
        await built._handler_result(binding, None, {})


def test_startup_probe_rejects_a_missing_prebuilt_response() -> None:
    built = Pipeline(Hinoki(startup_url="/health"))
    built.startup_response = None
    with pytest.raises(RuntimeError, match="startup response was not initialized"):
        built._startup_response(scope("GET", "/health"), {})
