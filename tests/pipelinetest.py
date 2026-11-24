"""The shared request pipeline, driven without the ASGI protocol."""

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from types import SimpleNamespace
from typing import Annotated, Any

import pytest

from hinoki import Field, Hinoki, Request, Schema, http
from hinoki.db import UniqueViolation
from hinoki.errors import Conflict
from hinoki.http import Middleware, MiddlewareRequest, Next, Stream
from hinoki.parsers import JSONParser
from hinoki.runtime.executor import ExecutorSaturated
from hinoki.runtime.inline import RequestDeadline
from hinoki.runtime.pipeline import NativeBody, Pipeline, native_input_exception
from hinoki.runtime.responses import Response
from hinoki.settings import AppSettings, ExecutionSettings


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


def test_pipeline_maps_an_unhandled_unique_violation_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = Pipeline(Hinoki())
    monkeypatch.delitem(pipeline.app.errors.defaults, UniqueViolation)
    assert isinstance(pipeline._default_error(UniqueViolation("duplicate")), Conflict)


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

    async def saturated(_binding: object, _args: object, _native: object) -> Response:
        raise ExecutorSaturated

    monkeypatch.setattr(built, "_call_native", saturated)
    response = await built._respond_native(built.bindings[0], "r" * 32, {}, None)
    assert response.status == 503


async def test_native_input_error_is_translated_through_the_error_registry() -> None:
    built = pipeline()
    response = await built.handle_native(
        built.bindings[0],
        "r" * 32,
        {},
        ("invalid_query", "query is invalid", None, []),
    )
    assert response.status == 400


async def test_native_internal_timeout_exception_uses_the_error_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = pipeline()

    async def timed_out(_binding: object, _args: object, _native: object) -> Response:
        raise TimeoutError("handler timeout")

    monkeypatch.setattr(built, "_call_native", timed_out)
    response = await built._respond_native(built.bindings[0], "r" * 32, {}, None)
    assert response.status == 500


def test_request_id_selection_and_final_header_replacement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built = pipeline()
    built.trust_request_id = True
    trusted_scope = scope("GET", "/ping", [(b"x-request-id", b"client-42")])
    assert built._request_id(trusted_scope, True) == "client-42"
    generated = built._request_id(scope("GET", "/ping"), False)
    assert len(generated) == 32
    assert generated != built._new_request_id()
    invalid = scope("GET", "/ping", [(b"x-request-id", b"bad id")])
    assert built._request_id(invalid, True) != "bad id"
    assert "ignored invalid request ID" in caplog.text
    response = built.with_headers(
        Response(200, b"ok", []), scope("GET", "/ping"), "r" * 32
    )
    assert header_map(response.headers)[b"content-length"] == b"2"


async def test_shared_respond_catches_handler_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = pipeline()

    async def broken(
        _scope: Any, _source: Any, _route: Any, **_kwargs: Any
    ) -> Response:
        raise RuntimeError("pipeline failure")

    monkeypatch.setattr(built, "_serve", broken)
    response = await built._respond(
        scope("GET", "/ping"), FakeBody(b""), "r" * 32, None
    )
    assert response.status == 500


async def test_internal_timeout_exception_is_translated_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = pipeline()

    async def timed_out(
        _scope: Any, _source: Any, _route: Any, **_kwargs: Any
    ) -> Response:
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


class Ticket(Schema):
    title: Annotated[str, Field(min_length=2)]
    seats: Annotated[int, Field(ge=1)]


@http.post("/tickets", status=201)
async def book(ticket: Ticket) -> dict[str, object]:
    return {"title": ticket.title, "seats": ticket.seats}


async def post_ticket(
    monkeypatch: pytest.MonkeyPatch, raw: bytes, typed: bool
) -> Response:
    if not typed:
        monkeypatch.setattr(Ticket, "validate_json", staticmethod(lambda _raw: None))
    app = Hinoki()
    app.include(book)
    built = Pipeline(app)
    built.lifespan._started = True
    headers = [(b"content-type", b"application/json")]
    body = NativeBody(FakeBody(raw), headers, b"", None)
    return await built.handle_native(built.bindings[0], "r" * 32, {}, None, body)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"title": "Opera", "seats": 2}',
        b'{"title": "O", "seats": 2}',
        b'{"title": "Opera", "seats": 0}',
        b'{"title": "Opera"}',
        b'{"title": "Opera", "seats": 2, "x": 1}',
        b'{"title": "Opera", "seats": 2',
    ],
)
async def test_typed_body_lane_answers_like_the_ordinary_path(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    fast = await post_ticket(monkeypatch, raw, True)
    slow = await post_ticket(monkeypatch, raw, False)
    assert (fast.status, fast.body) == (slow.status, slow.body)


RID = "r" * 32


async def admission_status(
    monkeypatch: pytest.MonkeyPatch, error: BaseException | None, params: Any
) -> int:
    built = pipeline()

    async def refuse(
        work: Coroutine[Any, Any, Any], *_args: Any, **_kwargs: Any
    ) -> Any:
        work.close()
        raise error  # type: ignore[misc]

    if error is not None:
        monkeypatch.setattr("hinoki.runtime.pipeline.run_inline", refuse)
    route: Any = SimpleNamespace()
    result = await built.admit_websocket(RID, route, scope("GET", "/ws"), params)
    assert isinstance(result, Response)
    return result.status


async def test_a_websocket_handshake_that_cannot_be_judged_is_an_http_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await admission_status(monkeypatch, None, None) == 400
    assert await admission_status(monkeypatch, RequestDeadline(), {}) == 504
    assert await admission_status(monkeypatch, ExecutorSaturated(), {}) == 503


def test_a_saturated_native_handler_is_answered_without_a_coroutine() -> None:
    answer = pipeline().native_failure_now(ExecutorSaturated(), RID)
    assert answer is not None
    assert answer.status == 503


def test_a_crud_failure_is_answered_in_place_when_no_handler_can_await() -> None:
    built = pipeline()
    timeout = built.crud_failure_now(0, RID, ("timeout", "slow", None, None, None))
    assert timeout is not None
    assert timeout.status == 504
    built.crud_plan_selections = [None]
    lost = built.crud_failure_now(0, RID, ("not_found", "gone", None, None, None))
    assert lost is not None
    assert lost.status == 500  # a failure without a plan is a server error


async def respond_with(status: int) -> Response:
    return Response(status)


async def fail_with(error: BaseException) -> Response:
    raise error


async def test_a_driven_handler_is_finished_under_the_remaining_timeout() -> None:
    built = pipeline()
    answered = await built.native_continue(RID, respond_with(200), 5.0)
    assert answered.status == 200
    late = await built.native_continue(RID, asyncio.sleep(5, Response(200)), 0.01)
    assert late.status == 504
    own = await built.native_continue(RID, fail_with(TimeoutError("upstream")), 5.0)
    assert own.status == 500  # not our deadline: translated like any error
    broken = await built.native_continue(RID, fail_with(ValueError("bad")), 5.0)
    assert broken.status == 500


async def test_a_cancelled_handler_only_unwinds_and_a_deadline_is_a_504() -> None:
    built = pipeline()
    done = await built.native_continue(RID, respond_with(201), None, "deadline")
    assert done.status == 201
    failed = await built.native_continue(
        RID, fail_with(ValueError("cleanup")), None, "deadline"
    )
    assert failed.status == 500
    cancelled = await built.native_continue(
        RID, fail_with(asyncio.CancelledError()), None, "deadline"
    )
    assert cancelled.status == 504


@pytest.mark.parametrize("reason", ["deadline", "shutdown"])
async def test_a_cancellation_that_is_not_ours_to_absorb_is_reraised(
    reason: str,
) -> None:
    built = pipeline()

    async def cancelled_from_outside() -> Response:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # an outside cancellation, counted by the task
        await asyncio.sleep(0)
        raise AssertionError("unreachable")

    async def runner() -> Response:
        return await built.native_continue(RID, cancelled_from_outside(), None, reason)

    task = asyncio.ensure_future(runner())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


def test_response_headers_that_cannot_be_hashed_are_still_shaped() -> None:
    built = pipeline()
    response = Response(200, b"x", [[b"x-list", b"1"]])  # type: ignore[list-item]
    headers, has_allow_origin = built.shaped_headers(response, False)
    assert (b"x-list", b"1") in headers
    assert not has_allow_origin
    assert not built.header_shapes  # an unhashable shape is not kept


def test_a_preflight_for_an_undecodable_path_uses_the_application_policy() -> None:
    @http.get("/shared", cors=http.Cors(origins=("https://p.example",)))
    async def shared() -> dict[str, str]:
        return {}

    built = Pipeline(Hinoki(include=[shared]))
    request = {"method": "OPTIONS", "path": "/shared", "raw_path": b"/sh\xffared"}
    chosen = built._preflight_policy(
        request,
        {"access-control-request-method": "GET"},
    )
    assert chosen is built.cors


def test_a_json_parser_with_extra_state_is_not_read_natively() -> None:
    parser = JSONParser()
    parser.extra = 1  # type: ignore[attr-defined]
    param = SimpleNamespace(schema=Ticket, text=False)
    assert Pipeline._typed_body(param, parser, b"{}") is None  # type: ignore[arg-type]


class Through(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Any:
        return await call_next()


class Replace(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Any:
        await call_next()
        return Stream(pieces())


async def pieces() -> AsyncIterator[bytes]:
    yield b"new"


@http.get("/replaced", middleware=[Replace()])
async def replaced() -> dict[str, str]:
    return {"ok": "plain"}


@http.get("/maybe")
async def maybe() -> Stream | dict[str, str]:
    return {"ok": "plain"}


async def serve_replaced(max_streams: int = 8) -> tuple[Pipeline, Response]:
    app = Hinoki(
        settings=AppSettings(execution=ExecutionSettings(max_streams=max_streams))
    )
    app.include(replaced)
    built = Pipeline(app)
    built.lifespan._started = True
    return built, await built.handle(scope("GET", "/replaced"), FakeBody(b""))


async def test_a_stream_a_middleware_creates_takes_a_place_of_its_own() -> None:
    _built, response = await serve_replaced()
    assert response.stream is not None
    assert response.stream.lease is not None
    response.stream.release()


async def test_a_full_worker_refuses_a_stream_a_middleware_creates() -> None:
    app = Hinoki(settings=AppSettings(execution=ExecutionSettings(max_streams=1)))
    app.include(replaced)
    built = Pipeline(app)
    built.lifespan._started = True
    held = built.streams.lease()
    assert held is not None
    response = await built.handle(scope("GET", "/replaced"), FakeBody(b""))
    assert response.status == 503
    held.release()


async def test_the_place_is_given_back_when_a_created_stream_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki(settings=AppSettings(execution=ExecutionSettings(max_streams=1)))
    app.include(replaced)
    built = Pipeline(app)
    built.lifespan._started = True

    async def broken(_answer: object) -> Response:
        raise RuntimeError("cannot start")

    monkeypatch.setattr(built, "_stream_response", broken)
    response = await built.handle(scope("GET", "/replaced"), FakeBody(b""))
    assert response.status == 500
    again = built.streams.lease()
    assert again is not None  # the single place was released
    again.release()


async def test_an_action_that_may_stream_gives_its_place_back_when_it_does_not() -> (
    None
):
    app = Hinoki(settings=AppSettings(execution=ExecutionSettings(max_streams=1)))
    app.include(maybe)
    built = Pipeline(app)
    built.lifespan._started = True
    for _ in range(2):  # a leaked place would turn the second answer into a 503
        response = await built.handle(scope("GET", "/maybe"), FakeBody(b""))
        assert response.status == 200


@http.get("/wrapped", middleware=[Through()])
async def wrapped() -> Stream:
    return Stream(pieces())


async def test_a_saturated_executor_is_a_503_even_through_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = Pipeline(Hinoki(include=[wrapped]))
    built.lifespan._started = True

    async def full(*_args: Any) -> Response:
        raise ExecutorSaturated

    monkeypatch.setattr(built, "_dispatch_request", full)
    response = await built.handle(scope("GET", "/wrapped"), FakeBody(b""))
    assert response.status == 503


async def test_a_stream_returned_through_middleware_is_kept_as_a_stream() -> None:
    built = Pipeline(Hinoki(include=[wrapped]))
    built.lifespan._started = True
    response = await built.handle(scope("GET", "/wrapped"), FakeBody(b""))
    assert response.stream is not None
    response.stream.release()


async def test_a_vetted_routed_request_skips_the_policies_its_transport_applied() -> (
    None
):
    app = Hinoki(settings=AppSettings(allowed_hosts=("api.example",)))
    app.include(ping)
    built = Pipeline(app)
    built.lifespan._started = True
    request = scope("GET", "/ping", [(b"host", b"other.example")])
    refused = await built.handle_unfinished(request, FakeBody(b""), "r" * 32, None)
    assert refused.status == 400
    routed = await built.handle_unfinished(
        request, FakeBody(b""), "r" * 32, (0, {}), vetted=True
    )
    assert (routed.status, json.loads(routed.body)) == (200, {"ok": True})
    unrouted = await built.handle_unfinished(
        request, FakeBody(b""), "r" * 32, None, vetted=True
    )
    assert unrouted.status == 400
