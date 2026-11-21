"""Sending streamed answers: the readers that advance the application's body,
the per-worker count of open streams and the pumps of the two transports.

On Kiso, Python only advances the application's iterator (``SourceReader``) and
hands each piece to a ``ResponseSink``; the sink's bounded queue, the idle and
keep-alive timers, the framing and the client-gone signal are Rust. A ``File``
goes to ``PreparedRequest.respond_file`` and its bytes never enter Python. The
ASGI pump crosses Python objects for every piece and has no native part.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from hinoki import core
from hinoki.http.stream import EventStream, File, Sourced, Streamed
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.responses import StreamHandle, StreamLease

log = logging.getLogger("hinoki.runtime")

CHUNK = 64 * 1024
END = object()
KEEPALIVE_FRAME = core.sse_frame(comment="")


class StreamAborted(RuntimeError):
    """A streamed body ended early; the ASGI server cuts the response."""


class SinkClosed(Exception):
    """The sink was closed by the other side while a piece was waiting."""


def locate(file: File) -> tuple[str, int] | None:
    """The resolved path and size of a servable file, or ``None``."""
    path = file.resolve()
    if path is None:
        return None
    try:
        return path, os.stat(path).st_size
    except OSError:
        return None


def declared_length(handle: StreamHandle) -> int | None:
    """The ``content-length`` known before the body is sent."""
    if handle.location is not None:
        return handle.location[1]
    answer = handle.answer
    return answer.length if isinstance(answer, Sourced) else None


class SourceReader:
    """Advances the application's iterable, one piece at a time: an async
    iterable on the event loop, a plain one on the sync executor."""

    __slots__ = ("answer", "executor", "iterator", "sync")

    def __init__(self, answer: Sourced, executor: SyncExecutor) -> None:
        self.answer = answer
        self.executor = executor
        self.iterator: Any = None
        self.sync = not hasattr(answer.source, "__aiter__")

    async def next(self) -> bytes | None:
        """The next piece as bytes; ``None`` when the body is complete."""
        if self.iterator is None:
            source: Any = self.answer.source
            self.iterator = iter(source) if self.sync else source.__aiter__()
        if self.sync:
            piece = await self.executor.run(next, self.iterator, END)
        else:
            try:
                piece = await anext(self.iterator)
            except StopAsyncIteration:
                return None
        return None if piece is END else self.answer.encode(piece)

    async def close(self) -> None:
        """Close the iterator (or the source, if it was never advanced)."""
        target = self.iterator if self.iterator is not None else self.answer.source
        closer = getattr(target, "aclose", None)
        if closer is not None:
            await closer()
            return
        sync_close = getattr(target, "close", None)
        if sync_close is not None:
            try:
                await self.executor.run(sync_close)
            except ValueError:  # still running on a worker thread
                log.debug("a stream source was still running when it was closed")


class FileReader:
    """Reads a file in chunks on the sync executor (when the server cannot
    send the file itself)."""

    __slots__ = ("executor", "file", "path")

    def __init__(self, path: str, executor: SyncExecutor) -> None:
        self.path = path
        self.executor = executor
        self.file: Any = None

    async def next(self) -> bytes | None:
        if self.file is None:
            self.file = await self.executor.run(open, self.path, "rb")
        chunk: bytes = await self.executor.run(self.file.read, CHUNK)
        return chunk or None

    async def close(self) -> None:
        if self.file is not None:
            await self.executor.run(self.file.close)


Reader = SourceReader | FileReader


def reader_for(handle: StreamHandle, executor: SyncExecutor) -> Reader:
    answer = handle.answer
    if isinstance(answer, Sourced):
        return SourceReader(answer, executor)
    if handle.location is None:
        raise RuntimeError("a File must be located before it is sent")
    return FileReader(handle.location[0], executor)


async def discard(handle: StreamHandle, executor: SyncExecutor) -> None:
    """Close the body of an answer that will not be sent, without advancing it,
    and give back its place."""
    try:
        if isinstance(handle.answer, Sourced):
            await SourceReader(handle.answer, executor).close()
    finally:
        handle.release()


async def discard_answer(answer: Streamed, executor: SyncExecutor) -> None:
    """``discard`` for an answer that holds no place yet."""
    await discard(StreamHandle(answer), executor)


class Guard:
    """Lets a watcher stop the task that pumps a body, and tells a stop the
    pump asked for from any other cancellation (shutdown)."""

    __slots__ = ("clean", "done", "gone", "task")

    def __init__(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("a stream is pumped from a task")
        self.task = task
        self.gone = False
        self.clean = False
        self.done = False

    def disconnected(self, watch: asyncio.Future[bool]) -> None:
        if watch.cancelled() or watch.exception() is not None or not watch.result():
            return
        self._stop(clean=False)

    def stop_clean(self) -> None:
        self._stop(clean=True)

    def _stop(self, *, clean: bool) -> None:
        if self.done:
            return
        self.gone, self.clean = not clean, clean
        self.task.cancel()

    def swallow(self) -> bool:
        """Whether the cancellation was ours; it is then consumed."""
        return (self.gone or self.clean) and self.task.uncancel() == 0


class Streams:
    """The worker's streams: the places in use, and the event streams that a
    shutdown ends cleanly."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.open = 0
        self.closing = False
        self.events: set[Guard] = set()

    def lease(self) -> StreamLease | None:
        if self.open >= self.limit:
            return None
        self.open += 1
        return StreamLease(self._free)

    def _free(self) -> None:
        self.open -= 1

    def enroll(self, guard: Guard) -> None:
        """Track an event stream; one that starts during shutdown ends at once."""
        self.events.add(guard)
        if self.closing:
            guard.stop_clean()

    def stop_events(self) -> None:
        """Begin shutdown: end every event stream with a normal close."""
        self.closing = True
        for guard in list(self.events):
            guard.stop_clean()


async def offer(sink: core.ResponseSink, piece: bytes) -> None:
    """Queue a piece, waiting while the sink's bounded queue is full."""
    while not sink.try_send(piece):
        if not await sink.ready():
            raise SinkClosed


async def feed(sink: core.ResponseSink, reader: Reader, preamble: bytes) -> None:
    if preamble:
        await offer(sink, preamble)
    while (piece := await reader.next()) is not None:
        await offer(sink, piece)


async def pump_sink(
    sink: core.ResponseSink, reader: Reader, preamble: bytes, streams: Streams | None
) -> None:
    """Move the body into the sink until it ends, the client goes away, or
    shutdown cancels the task. A failure after the head can only abort."""
    guard = Guard()
    watch: asyncio.Future[bool] = asyncio.ensure_future(sink.closed())
    watch.add_done_callback(guard.disconnected)
    if streams is not None:
        streams.enroll(guard)
    try:
        await feed(sink, reader, preamble)
        guard.done = True
        sink.finish()
    except asyncio.CancelledError:
        guard.done = True
        sink.finish() if guard.clean else sink.abort()
        if not guard.swallow():
            raise
    except SinkClosed:
        guard.done = True
    except Exception:
        guard.done = True
        log.exception("a streamed body failed; the response was cut")
        sink.abort()
    finally:
        guard.done = True
        watch.cancel()
        if streams is not None:
            streams.events.discard(guard)
        await reader.close()


async def watch_disconnect(receive: Callable[[], Awaitable[dict[str, Any]]]) -> bool:
    while (await receive())["type"] != "http.disconnect":
        pass
    return True


def body_message(piece: bytes, *, more: bool = True) -> dict[str, Any]:
    return {"type": "http.response.body", "body": piece, "more_body": more}


async def within(idle: float | None, reader: Reader) -> bytes | None:
    try:
        async with asyncio.timeout(idle) as limit:
            return await reader.next()
    except TimeoutError:
        if limit.expired():
            raise StreamAborted("a stream sent nothing for too long") from None
        raise


async def keeping_alive(
    reader: Reader, keepalive: float, send: Callable[[dict[str, Any]], Awaitable[None]]
) -> bytes | None:
    """The next piece; an SSE comment goes out after each quiet interval."""
    pending = asyncio.ensure_future(reader.next())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=keepalive)
            if done:
                return pending.result()
            await send(body_message(KEEPALIVE_FRAME))
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


class Pace(NamedTuple):
    """How a body is paced: what is sent first, the idle limit (a ``Stream``)
    and the keep-alive interval (an ``EventStream``)."""

    preamble: bytes
    idle: float | None
    keepalive: float | None
    events: bool


async def feed_asgi(
    send: Callable[[dict[str, Any]], Awaitable[None]], reader: Reader, pace: Pace
) -> None:
    if pace.preamble:
        await send(body_message(pace.preamble))
    while True:
        if pace.keepalive is None:
            piece = await within(pace.idle, reader)
        else:
            piece = await keeping_alive(reader, pace.keepalive, send)
        if piece is None:
            break
        if piece:
            await send(body_message(piece))
    await send(body_message(b"", more=False))


async def pump_asgi(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    reader: Reader,
    pace: Pace,
    streams: Streams,
) -> None:
    """The ASGI pump: a watcher on ``receive`` stops it when the client goes
    away, a failure raises ``StreamAborted`` so the server cuts the response."""
    guard = Guard()
    watch = asyncio.ensure_future(watch_disconnect(receive))
    watch.add_done_callback(guard.disconnected)
    if pace.events:
        streams.enroll(guard)
    try:
        await feed_asgi(send, reader, pace)
    except asyncio.CancelledError:
        guard.done = True
        if guard.clean:
            await send(body_message(b"", more=False))
        if not guard.swallow():
            raise
    except StreamAborted:
        raise
    except Exception as exc:
        log.exception("a streamed body failed; the response was cut")
        raise StreamAborted("a streamed body failed") from exc
    finally:
        guard.done = True
        watch.cancel()
        streams.events.discard(guard)
        await reader.close()


def keepalive_of(answer: Streamed, default: float | None) -> float | None:
    """The keep-alive interval of an event stream (``None``: off)."""
    if not isinstance(answer, EventStream):
        return None
    chosen = answer.keepalive
    return default if chosen is ... else chosen
