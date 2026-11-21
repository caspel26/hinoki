"""Stream, EventStream and File: registration, policies and the in-process client."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
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
from hinoki.runtime.nativelane import lane_refusal
from hinoki.runtime.streaming import (
    FileReader,
    Pace,
    SourceReader,
    StreamAborted,
    feed_asgi,
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


async def test_head_closes_the_source_without_reading_it() -> None:
    async with ASGIClient(build()) as client:
        reply = await client.request("HEAD", "/head")
        assert reply.status == 200
        assert not reply.body
        assert closed == ["head"]


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
