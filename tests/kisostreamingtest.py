"""Streamed responses over real loopback sockets with the Rust server."""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from hinoki import Hinoki, Request, http
from hinoki.http import Event, EventStream, File, Stream
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, ExecutionSettings

MIB = 1024 * 1024
state: dict[str, object] = {}


class Wire:
    """One client connection that reads a chunked response piece by piece."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.status = 0
        self.headers: dict[str, str] = {}

    @classmethod
    async def open(
        cls, port: int, target: str, method: str = "GET", extra: str = ""
    ) -> "Wire":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"{method} {target} HTTP/1.1\r\nhost: t\r\n{extra}\r\n".encode())
        wire = cls(reader, writer)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        lines = head.decode("latin-1").split("\r\n")
        wire.status = int(lines[0].split()[1])
        wire.headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        return wire

    async def chunk(self, wait: float = 5) -> bytes | None:
        """The next chunk; ``None`` after the terminator."""
        size = await asyncio.wait_for(self.reader.readuntil(b"\r\n"), wait)
        length = int(size.strip(), 16)
        data = await asyncio.wait_for(self.reader.readexactly(length + 2), wait)
        return data[:-2] if length else None

    async def rest(self) -> bytes:
        body = b""
        while (piece := await self.chunk()) is not None:
            body += piece
        return body

    def close(self) -> None:
        self.writer.close()


async def gated(label: str, gate: asyncio.Event) -> AsyncIterator[str]:
    yield "first;"
    await gate.wait()
    yield "last;"


async def endless(size: int) -> AsyncIterator[bytes]:
    try:
        while True:
            state["produced"] = int(str(state.get("produced", 0))) + 1
            yield b"x" * size
    finally:
        state["closed"] = True


def threads() -> Iterator[str]:
    yield threading.current_thread().name
    yield "|"
    yield threading.current_thread().name


@http.get("/gated")
async def gated_route() -> Stream:
    gate = state["gate"]
    assert isinstance(gate, asyncio.Event)
    return Stream(gated("g", gate), content_type="text/plain")


@http.get("/slow")
async def slow_route() -> Stream:
    return Stream(endless(256 * 1024))


@http.get("/broken")
async def broken_route() -> Stream:
    async def source() -> AsyncIterator[str]:
        yield "ok;"
        raise RuntimeError("boom")

    return Stream(source())


@http.get("/sync")
def sync_route() -> Stream:
    return Stream(threads(), content_type="text/plain")


def blocking() -> Iterator[str]:
    try:
        yield "first"
        release = state["release"]
        assert isinstance(release, threading.Event)
        release.wait(10)
        yield "second"
    finally:
        state["closed"] = True


@http.get("/blocking")
def blocking_route() -> EventStream:
    return EventStream(blocking(), keepalive=0.1)


@http.get("/silent")
async def silent_route() -> Stream:
    async def source() -> AsyncIterator[str]:
        yield "start;"
        await asyncio.sleep(30)

    return Stream(source())


@http.get("/events")
async def events_route(request: Request) -> EventStream:
    last = request.headers.get("last-event-id", "none")

    async def source() -> AsyncIterator[Event]:
        yield Event(last, event="resume")
        await asyncio.sleep(30)

    return EventStream(source())


@http.get("/watched")
async def watched_route() -> EventStream:
    async def source() -> AsyncIterator[Event]:
        try:
            yield Event("hello")
            await asyncio.sleep(30)
        finally:
            state["closed"] = True

    return EventStream(source())


@http.get("/file")
async def file_route() -> File:
    return File(str(state["file"]), filename="data.bin")


def app(**execution: object) -> Hinoki:
    settings = AppSettings(execution=ExecutionSettings(**execution))  # type: ignore[arg-type]
    built = Hinoki(settings=settings)
    built.include(
        gated_route,
        slow_route,
        blocking_route,
        broken_route,
        sync_route,
        silent_route,
        events_route,
        watched_route,
        file_route,
    )
    return built


@pytest.fixture(autouse=True)
def reset(tmp_path: Path) -> None:
    state.clear()
    state["gate"] = asyncio.Event()
    state["release"] = threading.Event()
    payload = tmp_path / "data.bin"
    payload.write_bytes(b"abc" * 1000)
    state["file"] = payload


async def serve(**execution: object) -> tuple[KisoWorker, int]:
    worker = KisoWorker(app(**execution), "127.0.0.1", 0)
    return worker, await worker.start()


async def test_pieces_reach_the_client_before_the_body_ends() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/gated")
        assert wire.status == 200
        assert wire.headers["transfer-encoding"] == "chunked"
        assert "content-length" not in wire.headers
        assert await wire.chunk() == b"first;"
        gate = state["gate"]
        assert isinstance(gate, asyncio.Event)
        gate.set()
        assert await wire.rest() == b"last;"
        wire.close()
    finally:
        await worker.stop()


async def test_a_slow_client_slows_the_producer_down() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/slow")
        await asyncio.sleep(0.5)
        first = int(str(state["produced"]))
        await asyncio.sleep(0.3)
        assert state["produced"] == first
        assert first * 256 * 1024 < 64 * MIB
        wire.close()
    finally:
        await worker.stop()


async def test_a_disconnect_closes_the_source() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/slow")
        assert await wire.chunk()
        wire.close()
        for _ in range(100):
            if state.get("closed"):
                break
            await asyncio.sleep(0.05)
        assert state.get("closed") is True
    finally:
        await worker.stop()


async def test_an_error_after_the_head_leaves_the_response_incomplete() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/broken")
        assert wire.status == 200
        assert await wire.chunk() == b"ok;"
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await wire.chunk()
        wire.close()
    finally:
        await worker.stop()


async def test_a_plain_iterator_is_advanced_off_the_event_loop() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/sync")
        body = (await wire.rest()).decode()
        names = [name for name in body.split("|") if name]
        assert names
        assert threading.current_thread().name not in names
        wire.close()
    finally:
        await worker.stop()


async def test_a_silent_stream_is_aborted_after_the_idle_timeout() -> None:
    worker, port = await serve(stream_idle_timeout=0.3)
    try:
        wire = await Wire.open(port, "/silent")
        assert await wire.chunk() == b"start;"
        started = time.monotonic()
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await wire.chunk()
        assert time.monotonic() - started < 3
        wire.close()
    finally:
        await worker.stop()


async def test_events_carry_keepalive_and_the_last_event_id() -> None:
    worker, port = await serve(sse_keepalive=0.1)
    try:
        wire = await Wire.open(port, "/events", extra="last-event-id: 41\r\n")
        assert wire.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert await wire.chunk() == b"event: resume\ndata: 41\n\n"
        assert await wire.chunk(wait=2) == b": \n\n"
        wire.close()
    finally:
        await worker.stop()


async def test_the_keepalive_notices_a_client_that_left_a_silent_stream() -> None:
    worker, port = await serve(sse_keepalive=0.1)
    try:
        wire = await Wire.open(port, "/watched")
        assert await wire.chunk()
        wire.close()
        for _ in range(60):
            if state.get("closed"):
                break
            await asyncio.sleep(0.05)
        assert state.get("closed") is True
    finally:
        await worker.stop()


async def test_shutdown_ends_an_open_event_stream_cleanly() -> None:
    worker, port = await serve()
    wire = await Wire.open(port, "/events")
    assert await wire.chunk()
    stopping = asyncio.create_task(worker.stop())
    assert await wire.rest() == b""
    await asyncio.wait_for(stopping, 10)
    wire.close()


async def test_a_full_worker_answers_503_and_retry_after() -> None:
    worker, port = await serve(max_streams=1)
    try:
        first = await Wire.open(port, "/gated")
        assert await first.chunk() == b"first;"
        refused = await Wire.open(port, "/gated")
        assert refused.status == 503
        assert refused.headers["retry-after"] == "1"
        refused.close()
        first.close()
        gate = state["gate"]
        assert isinstance(gate, asyncio.Event)
        gate.set()
    finally:
        await worker.stop()


async def test_a_file_is_sent_by_the_server_with_its_size() -> None:
    worker, port = await serve()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /file HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n")
        raw = await asyncio.wait_for(reader.read(), 5)
        head, _, body = raw.partition(b"\r\n\r\n")
        assert b"content-length: 3000" in head.lower()
        assert b'content-disposition: attachment; filename="data.bin"' in head.lower()
        assert body == b"abc" * 1000
        writer.close()
    finally:
        await worker.stop()


async def test_head_answers_the_head_and_no_body() -> None:
    worker, port = await serve()
    try:
        wire = await Wire.open(port, "/gated", method="HEAD")
        assert wire.status == 200
        wire.close()
    finally:
        await worker.stop()


async def test_a_blocked_sync_source_keeps_its_place_until_it_returns() -> None:
    worker, port = await serve(max_streams=1)
    release = state["release"]
    assert isinstance(release, threading.Event)
    try:
        wire = await Wire.open(port, "/blocking")
        assert await wire.chunk() == b"data: first\n\n"
        wire.close()
        await asyncio.sleep(0.3)
        refused = await Wire.open(port, "/blocking")
        assert refused.status == 503
        refused.close()
        assert state.get("closed") is None
        release.set()
        for _ in range(100):
            if state.get("closed"):
                break
            await asyncio.sleep(0.05)
        assert state.get("closed") is True
        await asyncio.sleep(0.1)
        again = await Wire.open(port, "/blocking")
        assert again.status == 200
        again.close()
    finally:
        release.set()
        await worker.stop()
