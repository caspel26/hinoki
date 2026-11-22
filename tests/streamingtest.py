"""Stream, EventStream and File: registration, policies and the in-process client."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hinoki import Hinoki, Schema, core, http
from hinoki.ai.mcpapp import ExposureError, select
from hinoki.db import DatabaseModule
from hinoki.errors import Conflict, RegistrationError
from hinoki.http import (
    Event,
    EventStream,
    File,
    Middleware,
    MiddlewareRequest,
    Next,
    Response,
    Stream,
)
from hinoki.runtime.executor import ExecutorSaturated
from hinoki.runtime.nativelane import lane_refusal
from hinoki.runtime.streaming import (
    KEEPALIVE_FRAME,
    FileReader,
    Guard,
    Pace,
    SourceReader,
    StreamAborted,
    Streams,
    feed_asgi,
    keeping_alive,
    next_piece,
    pump_asgi,
    pump_sink,
    reader_for,
    within,
)
from hinoki.settings import AppSettings, ExecutionSettings
from hinoki.testing import ASGIClient, BodyTooLarge, SyncClient
from hinoki.wsgi import WSGIAdapter, WSGIStartupError

closed: list[str] = []
calls: list[str] = []


class Params(Schema):
    n: int


async def counted(label: str, count: int = 3) -> AsyncIterator[str]:
    try:
        for index in range(count):
            yield f"{label}{index};"
            await asyncio.sleep(0)
    finally:
        closed.append(label)


async def forever(label: str) -> AsyncIterator[str]:
    try:
        index = 0
        while True:
            yield f"{label}{index};"
            index += 1
            await asyncio.sleep(0)
    finally:
        closed.append(label)


class Witness:
    """A source that is never advanced: only its ``aclose`` is observed."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __aiter__(self) -> "Witness":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        closed.append(self.label)


def lines() -> Iterator[str]:
    yield "a\n"
    yield "b\n"


@http.get("/count")
async def count() -> Stream:
    return Stream(counted("count"), content_type="text/plain")


@http.get("/sync")
def sync_lines() -> Stream:
    return Stream(lines(), content_type="text/plain")


@http.get("/union")
async def union() -> Stream | dict[str, int]:
    return Stream(counted("union"))


@http.get("/events")
async def events() -> EventStream:
    async def source() -> AsyncIterator[Event]:
        yield Event({"n": 1}, event="tick", id="1")
        yield Event("two")

    return EventStream(source(), retry=2500, keepalive=None)


@http.get("/forever")
async def endless() -> EventStream:
    return EventStream(forever("forever"), keepalive=None)


@http.get("/unannotated")
async def unannotated() -> dict[str, int]:
    return Stream(Witness("unannotated"))  # type: ignore[return-value]


@http.get("/validated")
async def validated(n: int) -> Stream:
    calls.append("handler")
    return Stream(counted("validated"))


@http.get("/broken")
async def broken() -> Stream:
    async def source() -> AsyncIterator[str]:
        yield "first;"
        raise RuntimeError("boom")

    return Stream(source())


@http.get("/swapped")
async def swapped() -> Stream:
    return Stream(counted("swapped"))


@http.get("/head")
async def head() -> Stream:
    return Stream(Witness("head"))


class Replace(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        await call_next()
        return Response(200, "replaced", content_type="text/plain")


class Mark(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        return (await call_next()).with_header("x-mark", "1")


@http.get("/replaced", middleware=[Replace()])
async def replaced() -> Stream:
    return Stream(Witness("replaced"))


@http.get("/marked", middleware=[Mark()])
async def marked() -> Stream:
    return Stream(counted("marked"))


@http.get("/file")
async def file_route() -> File:
    return File(Path(os.environ["STREAMING_FILES"]) / "a.txt", filename="a.txt")


def build(*extra: object, settings: AppSettings | None = None) -> Hinoki:
    app = Hinoki(settings=settings)
    app.include(
        count, sync_lines, union, events, endless, unannotated, validated, broken
    )
    app.include(swapped, head, replaced, marked, *extra)  # type: ignore[arg-type]
    return app


@pytest.fixture(autouse=True)
def reset() -> None:
    closed.clear()
    calls.clear()


def test_a_streamed_action_stays_off_the_native_lane() -> None:
    reasons = {b.route.path: lane_refusal(b) for b in build().bindings}
    assert reasons["/count"] == "streamed response"
    assert reasons["/union"] == "streamed response"
    assert reasons["/events"] == "streamed response"


def test_atomic_and_generator_handlers_are_refused_at_registration() -> None:
    @http.get("/bad-atomic", atomic=True)
    async def bad_atomic() -> Stream:
        return Stream(counted("x"))

    @http.get("/bad-generator")
    async def bad_generator():  # type: ignore[no-untyped-def]
        yield "x"

    @http.get("/bad-sync-generator")
    def bad_sync_generator():  # type: ignore[no-untyped-def]
        yield "x"

    @http.get("/bad-status", status=204)
    async def bad_status() -> Stream:
        return Stream(counted("x"))

    for handler, text in (
        (bad_atomic, "atomic"),
        (bad_generator, "generator"),
        (bad_sync_generator, "generator"),
        (bad_status, "204"),
    ):
        with pytest.raises(RegistrationError, match=text):
            Hinoki().include(handler)


async def test_streams_are_sent_whole_through_get_and_in_pieces_through_stream() -> (
    None
):
    async with ASGIClient(build()) as client:
        whole = await client.get("/count")
        assert (whole.status, whole.body) == (200, "count0;count1;count2;")
        assert whole.headers["content-type"] == "text/plain"
        async with client.stream("GET", "/count") as reply:
            assert reply.status == 200
            pieces = [piece async for piece in reply.chunks()]
        assert b"".join(pieces) == b"count0;count1;count2;"
        assert len(pieces) == 3
        assert (await client.get("/sync")).body == "a\nb\n"
        assert (await client.get("/union")).body == "union0;union1;union2;"


async def test_an_event_stream_is_framed_and_labelled() -> None:
    async with ASGIClient(build()) as client:
        reply = await client.get("/events")
        assert reply.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-accel-buffering"] == "no"
        assert reply.body == (
            'retry: 2500\n\nevent: tick\nid: 1\ndata: {"n":1}\n\ndata: two\n\n'
        )


async def test_a_stream_from_an_unannotated_action_is_a_server_error() -> None:
    async with ASGIClient(build()) as client:
        reply = await client.get("/unannotated")
        assert reply.status == 500
        await asyncio.sleep(0.05)
        assert closed == ["unannotated"]


async def test_a_full_worker_answers_503_before_the_handler_runs() -> None:
    app = build(settings=AppSettings(execution=ExecutionSettings(max_streams=1)))
    async with ASGIClient(app) as client:
        async with client.stream("GET", "/forever"):
            busy = await client.get("/validated", query="n=1")
            assert busy.status == 503
            assert busy.headers["retry-after"] == "1"
            assert calls == []
        await asyncio.sleep(0.05)
        assert (await client.get("/validated", query="n=1")).status == 200
        assert calls == ["handler"]


async def test_validation_errors_on_a_streamed_action_are_plain_json() -> None:
    async with ASGIClient(build()) as client:
        reply = await client.get("/validated", query="n=x")
        assert reply.status == 422
        assert reply.headers["content-type"].startswith("application/json")
        assert calls == []


async def test_an_atomic_action_that_is_not_streamed_still_answers_its_error(
    tmp_path: Path,
) -> None:
    @http.post("/taken", atomic=True)
    async def taken() -> dict[str, int]:
        raise Conflict()

    app = Hinoki(modules=[DatabaseModule(f"sqlite://{tmp_path / 'a.sqlite3'}")])
    app.include(taken)
    async with ASGIClient(app) as client:
        assert (await client.post("/taken")).status == 409


async def test_middleware_keeps_the_stream_and_closes_a_replaced_one() -> None:
    async with ASGIClient(build()) as client:
        marked_reply = await client.get("/marked")
        assert marked_reply.headers["x-mark"] == "1"
        assert marked_reply.body == "marked0;marked1;marked2;"
        replaced_reply = await client.get("/replaced")
        assert replaced_reply.body == "replaced"
        assert "replaced" in closed


async def test_an_error_after_the_head_cuts_the_response() -> None:
    async with (
        ASGIClient(build()) as client,
        client.stream("GET", "/broken") as reply,
    ):
        assert reply.status == 200
        pieces = reply.chunks()
        assert await anext(pieces) == b"first;"
        with pytest.raises(Exception):  # noqa: B017, PT011
            await anext(pieces)


async def test_leaving_the_stream_disconnects_and_closes_the_source() -> None:
    async with ASGIClient(build()) as client:
        async with client.stream("GET", "/forever") as reply:
            assert await anext(reply.chunks())
        await asyncio.sleep(0.1)
        assert "forever" in closed


async def test_get_on_an_endless_stream_fails_with_a_byte_limit() -> None:
    async with ASGIClient(build()) as client:
        with pytest.raises(BodyTooLarge):
            await client.get("/forever", max_bytes=1024)
        await asyncio.sleep(0.1)
        assert "forever" in closed


def test_the_sync_client_streams_too() -> None:
    with SyncClient(build()) as client, client.stream("GET", "/count") as reply:
        assert reply.status == 200
        assert next(reply.chunks()) == b"count0;"
        assert reply.read() == b"count1;count2;"


def test_the_openapi_document_describes_the_bodies(tmp_path: Path) -> None:
    os.environ["STREAMING_FILES"] = str(tmp_path)
    paths = build(file_route).openapi()["paths"]
    stream = paths["/count"]["get"]["responses"]["200"]["content"]
    assert stream["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    sse = paths["/events"]["get"]["responses"]["200"]["content"]
    assert sse == {"text/event-stream": {"schema": {"type": "string"}}}
    assert (
        "application/octet-stream"
        in (paths["/file"]["get"]["responses"]["200"]["content"])
    )


def test_the_openapi_document_keeps_the_plain_arms_of_a_union() -> None:
    content = build().openapi()["paths"]["/union"]["get"]["responses"]["200"]["content"]
    assert set(content) == {"application/octet-stream", "application/json"}
    assert content["application/json"]["schema"]["type"] == "object"


def test_the_application_mcp_refuses_a_streamed_action() -> None:
    with pytest.raises(ExposureError, match="streams its response"):
        select(build(), ["*"], False, "/mcp")


def test_the_wsgi_adapter_refuses_streams_at_start() -> None:
    adapter = WSGIAdapter(build())
    with pytest.raises(WSGIStartupError, match="count"):
        adapter.start()


@pytest.fixture
def files(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    (root / "sub").mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    return root


async def test_a_file_is_sent_with_its_size_and_name(files: Path) -> None:
    @http.get("/a")
    async def a() -> File:
        return File("a.txt", root=files, filename="a.txt")

    async with ASGIClient(_app(a)) as client:
        reply = await client.get("/a")
        assert (reply.status, reply.body) == (200, "hello")
        assert reply.headers["content-length"] == "5"
        assert reply.headers["content-disposition"] == 'attachment; filename="a.txt"'
        assert reply.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "name", ["../secret.txt", "link.txt", "sub", "missing.txt", "/etc/hosts"]
)
async def test_a_file_outside_the_root_or_not_a_file_is_not_found(
    files: Path, name: str
) -> None:
    @http.get("/f")
    async def f() -> File:
        return File(name, root=files)

    async with ASGIClient(_app(f)) as client:
        assert (await client.get("/f")).status == 404


async def test_locate_file_follows_links_inside_the_root_and_reports_the_size(
    files: Path,
) -> None:
    (files / "alias.txt").symlink_to(files / "a.txt")
    found = await core.locate_file("alias.txt", str(files))
    assert found == (str((files / "a.txt").resolve()), 5)
    assert await core.locate_file(str(files / "a.txt")) == found
    assert await core.locate_file("link.txt", str(files)) is None


async def test_a_name_that_is_not_utf8_is_found_where_the_file_system_allows_it(
    files: Path,
) -> None:
    name = b"raw\xff.txt"
    try:
        (files / os.fsdecode(name)).write_bytes(b"x")
    except (OSError, ValueError):
        pytest.skip("the file system refuses names that are not UTF-8")
    found = await core.locate_file(os.fsdecode(name), str(files))
    assert found is not None
    assert found[1] == 1


async def test_a_full_set_of_file_lookups_answers_503_and_retry_after(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_: object) -> None:
        raise core.FileLookupBusy("full")

    monkeypatch.setattr(core, "locate_file", busy)

    @http.get("/f")
    async def f() -> File:
        return File("a.txt", root=files)

    async with ASGIClient(_app(f)) as client:
        response = await client.get("/f")
    assert response.status == 503
    assert response.headers["retry-after"] == "1"


async def test_head_closes_the_source_without_reading_it() -> None:
    async with ASGIClient(build()) as client:
        reply = await client.request("HEAD", "/head")
        assert reply.status == 200
        assert not reply.body
        assert closed == ["head"]


@pytest.mark.parametrize("name", ["", "a/b", "a\nb"])
def test_a_download_name_with_a_slash_or_a_control_character_is_refused(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="filename"):
        File("x", filename=name)


def test_a_download_name_keeps_its_exact_utf8_form() -> None:
    headers = dict(File("x", filename="résumé.pdf").headers)
    assert headers[b"content-disposition"] == (
        b"attachment; filename=\"r_sum_.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
    )


def test_a_stream_place_is_counted_in_core_and_given_back_when_dropped() -> None:
    places = core.StreamLimit(1)
    permit = places.acquire()
    assert permit is not None
    assert places.acquire() is None
    assert places.open == 1
    del permit
    assert places.open == 0
    with pytest.raises(ValueError, match="at least 1"):
        core.StreamLimit(0)


class Inline:
    """An executor that runs the call at once."""

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return fn(*args)


async def test_a_body_longer_than_its_length_is_cut_at_the_length() -> None:
    sent: list[bytes] = []
    with pytest.raises(StreamAborted):
        await _feed(b"abcdefgh", 5, sent)
    assert b"".join(sent) == b"abcde"


async def test_a_body_shorter_than_its_length_is_aborted_not_completed() -> None:
    sent: list[bytes] = []
    with pytest.raises(StreamAborted):
        await _feed(b"ab", 5, sent)
    assert sent == [b"ab"]


async def test_a_body_of_exactly_its_length_ends_complete() -> None:
    sent: list[bytes] = []
    await _feed(b"abcde", 5, sent)
    assert sent == [b"abcde", b""]


async def test_a_file_reader_never_reads_past_the_size_it_saw(tmp_path: Path) -> None:
    path = tmp_path / "grown.bin"
    path.write_bytes(b"0123456789")
    reader = FileReader(str(path), 4, Inline())  # type: ignore[arg-type]
    body = b""
    while (piece := await reader.next()) is not None:
        body += piece
    await reader.close()
    assert body == b"0123"


async def _feed(source: bytes, length: int, sent: list[bytes]) -> None:
    async def send(message: dict[str, Any]) -> None:
        sent.append(message["body"])

    reader = SourceReader(Stream(iter([source]), length=length), Inline())  # type: ignore[arg-type]
    await feed_asgi(send, reader, Pace(b"", None, None, False, length))


def _app(*handlers: object) -> Hinoki:
    app = Hinoki()
    app.include(*handlers)  # type: ignore[arg-type]
    return app


class Saturated:
    """An executor that refuses or fails every call with ``error``."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        raise self.error


class Gated:
    """A reader that waits for ``gate`` before each piece, then ends."""

    def __init__(self, pieces: list[bytes] | None = None) -> None:
        self.gate = asyncio.Event()
        self.pieces = pieces or []
        self.closed = False
        self.cancelled = False

    async def next(self) -> bytes | None:
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.pieces.pop(0) if self.pieces else None

    async def close(self) -> None:
        self.closed = True


class FakeSink:
    def __init__(self, accepts: bool = True) -> None:
        self.accepts = accepts
        self.sent: list[bytes] = []
        self.ended: str | None = None
        self.gone: asyncio.Future[bool] = asyncio.Future()

    def try_send(self, piece: bytes) -> bool:
        self.sent.append(piece)
        return self.accepts

    async def ready(self) -> bool:
        return self.accepts

    async def closed(self) -> bool:
        return await self.gone

    def finish(self) -> None:
        self.ended = "finish"

    def abort(self) -> None:
        self.ended = "abort"


async def test_a_source_still_running_or_an_executor_full_does_not_fail_the_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="hinoki.runtime")

    def pieces() -> Iterator[bytes]:
        yield b"a"

    busy = SourceReader(Stream(pieces()), Saturated(ValueError("running")))  # type: ignore[arg-type]
    await busy.close()
    assert "still running when it was closed" in caplog.text
    full = SourceReader(Stream(pieces()), Saturated(ExecutorSaturated()))  # type: ignore[arg-type]
    await full.close()
    assert "not closed: the executor is full" in caplog.text


async def test_a_file_reader_that_never_opened_has_nothing_to_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unopened = FileReader("x", 1, Saturated(ExecutorSaturated()))  # type: ignore[arg-type]
    await unopened.close()
    assert caplog.text == ""
    opened = FileReader("x", 1, Saturated(ExecutorSaturated()))  # type: ignore[arg-type]
    opened.file = SimpleNamespace(close=lambda: None)
    await opened.close()
    assert "a file was not closed" in caplog.text


def test_a_file_without_a_location_cannot_be_read() -> None:
    handle = SimpleNamespace(answer=object(), location=None)
    with pytest.raises(RuntimeError, match="must be located"):
        reader_for(handle, Inline())  # type: ignore[arg-type]


async def test_a_guard_needs_a_running_task() -> None:
    outcome: asyncio.Future[BaseException | None] = asyncio.Future()

    def from_a_callback() -> None:
        try:
            Guard()
        except RuntimeError as error:
            outcome.set_result(error)

    asyncio.get_running_loop().call_soon(from_a_callback)
    assert "pumped from a task" in str(await outcome)


async def test_an_event_stream_that_starts_during_shutdown_ends_at_once() -> None:
    streams = Streams(1)
    streams.stop_events()
    seen: list[Guard] = []

    async def start() -> None:
        guard = Guard()
        seen.append(guard)
        streams.enroll(guard)
        await asyncio.sleep(10)

    task = asyncio.ensure_future(start())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[0].clean


async def test_a_pumped_body_starts_with_its_preamble_and_ends_finished() -> None:
    sink, reader = FakeSink(), Gated([b"a", b"b"])
    reader.gate.set()
    await pump_sink(sink, reader, b"pre", None)  # type: ignore[arg-type]
    assert sink.sent == [b"pre", b"a", b"b"]
    assert sink.ended == "finish" and reader.closed


async def test_shutdown_cancelling_a_pump_aborts_the_sink_and_propagates() -> None:
    sink, reader = FakeSink(), Gated()
    task = asyncio.ensure_future(pump_sink(sink, reader, b"", None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.ended == "abort" and reader.closed and reader.cancelled


async def test_a_stream_idle_for_too_long_is_aborted_but_a_reader_timeout_is_not() -> (
    None
):
    with pytest.raises(StreamAborted, match="nothing for too long"):
        await within(0.01, Gated())  # type: ignore[arg-type]

    class Failing(Gated):
        async def next(self) -> bytes | None:
            raise TimeoutError("the source's own")

    with pytest.raises(TimeoutError, match="the source's own"):
        await within(5, Failing())  # type: ignore[arg-type]


async def test_a_quiet_event_stream_sends_comments_until_the_next_event() -> None:
    reader = Gated([b"event"])
    sent: list[dict[str, Any]] = []
    quiet = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        quiet.set()

    pace = Pace(b"", None, 0.01, True)
    task = asyncio.ensure_future(next_piece(send, reader, pace))  # type: ignore[arg-type]
    await asyncio.wait_for(quiet.wait(), timeout=5)
    reader.gate.set()
    assert await task == b"event"
    assert sent[0]["body"] == KEEPALIVE_FRAME and sent[0]["more_body"] is True


async def test_cancelling_a_keep_alive_wait_cancels_the_pending_piece() -> None:
    reader = Gated()

    async def send(message: dict[str, Any]) -> None:
        return None

    task = asyncio.ensure_future(keeping_alive(reader, 5, send))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.cancelled


async def never() -> dict[str, Any]:
    await asyncio.Event().wait()
    return {}


async def test_shutdown_ends_an_asgi_event_stream_with_a_final_empty_body() -> None:
    reader, streams = Gated(), Streams(1)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    pace = Pace(b"", None, None, True)
    task = asyncio.ensure_future(pump_asgi(send, never, reader, pace, streams))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    streams.stop_events()
    await task
    assert sent[-1]["more_body"] is False and reader.closed


async def test_cancelling_an_asgi_pump_from_outside_propagates() -> None:
    reader, streams = Gated(), Streams(1)

    async def send(message: dict[str, Any]) -> None:
        return None

    pace = Pace(b"", None, None, False)
    task = asyncio.ensure_future(pump_asgi(send, never, reader, pace, streams))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.closed


async def test_an_aborted_asgi_body_reaches_the_server_unchanged() -> None:
    class Aborting(Gated):
        async def next(self) -> bytes | None:
            raise StreamAborted("cut")

    async def send(message: dict[str, Any]) -> None:
        return None

    pace = Pace(b"", None, None, False)
    with pytest.raises(StreamAborted, match="cut"):
        await pump_asgi(send, never, Aborting(), pace, Streams(1))  # type: ignore[arg-type]


async def test_shutdown_finishes_a_pumped_event_stream_cleanly() -> None:
    sink, reader, streams = FakeSink(), Gated(), Streams(1)
    task = asyncio.ensure_future(pump_sink(sink, reader, b"", streams))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    streams.stop_events()
    await task
    assert sink.ended == "finish" and not streams.events and reader.closed


async def test_a_client_that_leaves_aborts_the_pump_without_cancelling_the_caller() -> (
    None
):
    sink, reader = FakeSink(), Gated()
    task = asyncio.ensure_future(pump_sink(sink, reader, b"", None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    sink.gone.set_result(True)
    await task
    assert sink.ended == "abort" and reader.closed


async def test_a_sink_closed_while_a_piece_waits_ends_the_pump_quietly() -> None:
    sink, reader = FakeSink(accepts=False), Gated([b"a"])
    reader.gate.set()
    await pump_sink(sink, reader, b"", None)  # type: ignore[arg-type]
    assert sink.ended is None and reader.closed


async def test_a_failing_source_cuts_the_response_after_the_head(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing(Gated):
        async def next(self) -> bytes | None:
            raise ValueError("source broke")

    sink, reader = FakeSink(), Failing()
    await pump_sink(sink, reader, b"", None)  # type: ignore[arg-type]
    assert sink.ended == "abort" and "the response was cut" in caplog.text


async def test_a_guard_whose_pump_is_done_ignores_a_late_stop() -> None:
    survived = asyncio.Event()

    async def pump() -> None:
        guard = Guard()
        guard.done = True
        guard.stop_clean()
        await asyncio.sleep(0)
        survived.set()

    await asyncio.ensure_future(pump())
    assert survived.is_set()


async def test_closing_a_source_waits_for_its_pending_piece() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield b"a"

    reader = SourceReader(Stream(source()), Inline())  # type: ignore[arg-type]
    ran = asyncio.Event()

    async def slow() -> None:
        await asyncio.sleep(0)
        ran.set()

    reader.pending = asyncio.ensure_future(slow())
    await reader.close()
    assert ran.is_set() and reader.pending is None
