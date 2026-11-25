"""An in-process client for tests: drives the application through the ASGI
lifespan and http protocols, with no sockets and no server process.

    async with ASGIClient(app) as client:
        reply = await client.post("/tasks", json={"title": "x"}, token="abc")
        assert reply.status == 201

A sync twin, ``SyncClient``, exists for tests without ``async``. The lifespan
runs, so a configured ``DatabaseModule`` opens its pool: point the
app at a disposable database. ``reply.body`` is decoded JSON when the response
is JSON, else text.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, TypeVar

from hinoki import core
from hinoki.app import Hinoki
from hinoki.runtime.asgi import IN_PROCESS
from hinoki.utils import _read_utc

HeaderInput = Mapping[str, str] | Sequence[tuple[str, str]]
FormInput = Mapping[str, str] | Sequence[tuple[str, str]]
FileInput = Sequence[tuple[str, str, bytes, str]]

ERROR_STATUS = 400
MAX_BYTES = 10 * 1024 * 1024  # what ``client.get()`` and friends collect, by default
STREAM_PIECES = 8  # pieces a stream may run ahead of the test that reads it
STREAM_CLOSE_TIMEOUT = 5.0
T = TypeVar("T")
# What the framework itself answers, whatever an action declares.
FRAMEWORK_CODES = frozenset(
    {
        "validation_error",
        "unauthorized",
        "forbidden",
        "payload_too_large",
        "unsupported_media_type",
        "not_acceptable",
        "invalid_json",
        "invalid_form",
        "invalid_multipart",
        "invalid_content_type",
        "invalid_query",
        "internal_error",
        "overloaded",
        "rate_limit_unavailable",
        "timeout",
        "not_ready",
        "disallowed_host",
        "cors_origin_denied",
    }
)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any
    raw: bytes = b""  # the body exactly as sent


class ManualClock:
    """A thread-safe, controllable UTC clock for deterministic tests."""

    def __init__(self, start: datetime) -> None:
        self._value = _read_utc(lambda: start)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._value

    def set(self, value: datetime) -> None:
        """Set the current instant; ``value`` must be timezone-aware."""
        instant = _read_utc(lambda: value)
        with self._lock:
            self._value = instant

    def advance(self, *, seconds: float = 0) -> datetime:
        """Advance by elapsed seconds and return the new UTC instant."""
        with self._lock:
            self._value += timedelta(seconds=seconds)
            return self._value


class Feed:
    """The ASGI ``receive`` of one request: the body once, then (like a server)
    nothing until the client disconnects."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.delivered = False
        self.gone = asyncio.Event()

    async def __call__(self) -> dict[str, Any]:
        if not self.delivered:
            self.delivered = True
            return {"type": "http.request", "body": self.body, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}


class BodyTooLarge(RuntimeError):
    """A response passed the ``max_bytes`` a test client collects; read a
    streamed response with ``client.stream()``."""


class StreamReply:
    """The head of a streamed response and its body, piece by piece."""

    def __init__(
        self, status: int, headers: dict[str, str], pieces: AsyncIterator[bytes]
    ) -> None:
        self.status = status
        self.headers = headers
        self._pieces = pieces

    def chunks(self) -> AsyncIterator[bytes]:
        """The body pieces as the server sent them; the iterator raises the
        error that cut the response, if one did."""
        return self._pieces

    async def read(self, max_bytes: int | None = MAX_BYTES) -> bytes:
        """The rest of the body (``BodyTooLarge`` past ``max_bytes``)."""
        received = bytearray()
        async for piece in self._pieces:
            received += piece
            if max_bytes is not None and len(received) > max_bytes:
                raise BodyTooLarge(f"the body passed {max_bytes} bytes")
        return bytes(received)


class ASGIClient:
    """Use as an async context manager so startup and shutdown always run."""

    def __init__(
        self, app: Hinoki, strict_errors: bool = False, *, host: str | None = None
    ) -> None:
        self.app = app
        self.strict_errors = strict_errors
        self.host = host
        self.adapter = app.asgi()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.lifespan: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ASGIClient:
        self.lifespan = asyncio.create_task(
            self.adapter(
                {"type": "lifespan", IN_PROCESS: True}, self.inbox.get, self.outbox.put
            )
        )
        await self.inbox.put({"type": "lifespan.startup"})
        started = await self.outbox.get()
        if started["type"] != "lifespan.startup.complete":
            await self.lifespan
            raise RuntimeError(f"startup failed: {started.get('message')}")
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self.lifespan is not None:
            await self.inbox.put({"type": "lifespan.shutdown"})
            await self.outbox.get()
            await self.lifespan

    def _build(  # noqa: PLR0913 - request inputs are named public options
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        body: bytes = b"",
        data: FormInput | None = None,
        files: FileInput | None = None,
        query: str = "",
        headers: HeaderInput | None = None,
        token: str | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        """The ASGI scope and the body of one test request. ``ASGIClient`` and
        ``SyncClient`` (``request`` and ``stream``) all build their scope here."""
        if "?" in path or "#" in path:
            raise ValueError(
                f"{method} {path!r}: put the query string in `query=`, "
                "not in the path (and leave out any #fragment)"
            )
        sent = _header_items(headers)
        if token is not None:
            sent = [(name, value) for name, value in sent if name != "authorization"]
            sent.append(("authorization", f"Bearer {token}"))
        body, sent = _request_body(json, body, data, files, sent)
        if self.host is not None and not any(name == "host" for name, _ in sent):
            sent.append(("host", self.host))
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [(k.encode(), v.encode()) for k, v in sent],
        }
        return scope, body

    async def request(  # noqa: PLR0913 - request inputs are named public options
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        body: bytes = b"",
        data: FormInput | None = None,
        files: FileInput | None = None,
        query: str = "",
        headers: HeaderInput | None = None,
        token: str | None = None,
        max_bytes: int | None = MAX_BYTES,
    ) -> Response:
        """One request, including JSON or multipart test payloads. The whole
        response is collected; past ``max_bytes`` of body (``None``: no limit)
        the client disconnects and raises ``BodyTooLarge``, so an endless
        stream cannot hang a test (read one with ``client.stream()``)."""
        scope, payload = self._build(
            method,
            path,
            json=json,
            body=body,
            data=data,
            files=files,
            query=query,
            headers=headers,
            token=token,
        )
        feed = Feed(payload)
        sink: list[dict[str, Any]] = []
        total = 0

        async def send(message: dict[str, Any]) -> None:
            nonlocal total
            sink.append(message)
            total += len(message.get("body", b""))
            if max_bytes is not None and total > max_bytes:
                feed.gone.set()
                await asyncio.sleep(0.001)  # lets the server see the disconnect

        await self.adapter(scope, feed, send)
        if max_bytes is not None and total > max_bytes:
            raise BodyTooLarge(
                f"{method} {path} sent more than {max_bytes} bytes; "
                "read a streamed response with client.stream()"
            )
        start = next(m for m in sink if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in sink if m["type"].endswith("body"))
        reply_headers = {k.decode(): v.decode() for k, v in start["headers"]}
        text: Any = raw.decode("utf-8", "replace") or None
        if raw and reply_headers.get("content-type", "").endswith("json"):
            text = _loads(raw)
        reply = Response(start["status"], reply_headers, text, raw)
        if self.strict_errors:
            self.check_declared(method, path, reply)
        return reply

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, **options: Any
    ) -> AsyncIterator[StreamReply]:
        """A response read as it is produced. The head is available on entry;
        leaving the block disconnects the client, which ends the stream:

            async with client.stream("GET", "/events") as reply:
                async for piece in reply.chunks():
                    ...

        A server that runs ahead of the test waits for it (at most
        ``STREAM_PIECES`` pieces are buffered)."""
        scope, payload = self._build(method, path, **options)
        run = _StreamRun(self.adapter, scope, Feed(payload))
        try:
            yield await run.head()
        finally:
            await run.close()

    def check_declared(self, method: str, path: str, reply: Response) -> None:
        """With ``strict_errors=True``: an error answer whose code the action did
        not declare (``errors=...``) and the framework does not produce by itself
        is an ``AssertionError``. Catches business errors missing from OpenAPI."""
        if reply.status < ERROR_STATUS or not isinstance(reply.body, dict):
            return
        found = self.app.resolve(method, path)
        if found is None:
            return  # unrouted (404/405) or a documentation page
        route = found[0]
        code = reply.body.get("error")
        declared = {e.code for e in route.errors}
        if code not in declared | FRAMEWORK_CODES:
            raise AssertionError(
                f"{method} {path} answered {reply.status} {code!r}, which "
                f"{route.name} does not declare: add it to errors=(...)"
            )

    async def get(self, path: str, **options: Any) -> Response:
        return await self.request("GET", path, **options)

    async def post(self, path: str, **options: Any) -> Response:
        return await self.request("POST", path, **options)

    async def put(self, path: str, **options: Any) -> Response:
        return await self.request("PUT", path, **options)

    async def patch(self, path: str, **options: Any) -> Response:
        return await self.request("PATCH", path, **options)

    async def delete(self, path: str, **options: Any) -> Response:
        return await self.request("DELETE", path, **options)

    async def head(self, path: str, **options: Any) -> Response:
        return await self.request("HEAD", path, **options)

    async def options(self, path: str, **options: Any) -> Response:
        return await self.request("OPTIONS", path, **options)

    async def query(self, path: str, **options: Any) -> Response:
        """An HTTP QUERY request (``json=`` is its body; the ``query=`` option
        is still the query string)."""
        return await self.request("QUERY", path, **options)


class _StreamRun:
    """One request whose response is read as the application produces it: the
    messages wait in a queue the reader empties, and the application waits
    when it runs ``STREAM_PIECES`` ahead."""

    def __init__(self, adapter: Any, scope: dict[str, Any], feed: Feed) -> None:
        self.feed = feed
        self.messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.slots = asyncio.Semaphore(STREAM_PIECES)
        self.closing = False
        self.failure: Exception | None = None
        self.task = asyncio.create_task(self._run(adapter, scope))

    async def _run(self, adapter: Any, scope: dict[str, Any]) -> None:
        try:
            await adapter(scope, self.feed, self._send)
        except Exception as exc:
            self.failure = exc
        finally:
            self.messages.put_nowait(None)

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.closing:
            await self.slots.acquire()
        self.messages.put_nowait(message)

    async def head(self) -> StreamReply:
        message = await self.messages.get()
        if message is None or message["type"] != "http.response.start":
            raise self.failure or RuntimeError("the application sent no response")
        self.slots.release()
        headers = {k.decode(): v.decode() for k, v in message["headers"]}
        return StreamReply(message["status"], headers, self._pieces())

    async def _pieces(self) -> AsyncIterator[bytes]:
        while True:
            message = await self.messages.get()
            if message is None:
                if self.failure is not None:
                    raise self.failure  # the response was cut
                return
            self.slots.release()
            if piece := message.get("body", b""):
                yield piece
            if not message.get("more_body", False):
                return

    async def close(self) -> None:
        """Disconnect the client and wait for the application to wind down."""
        self.closing = True
        self.feed.gone.set()
        done, _ = await asyncio.wait({self.task}, timeout=STREAM_CLOSE_TIMEOUT)
        if not done:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def _dumps(value: Any) -> bytes:
    return core.encode_json(value)


def _loads(raw: bytes) -> Any:
    return core.decode_json(raw)


def _request_body(
    json_value: Any,
    body: bytes,
    data: FormInput | None,
    files: FileInput | None,
    headers: list[tuple[str, str]],
) -> tuple[bytes, list[tuple[str, str]]]:
    """Choose one body encoding and its content type for a test request."""
    if data is not None or files is not None:
        if json_value is not None or body:
            raise ValueError("multipart data cannot be combined with json= or body=")
        if any(name == "content-type" for name, _ in headers):
            raise ValueError("content-type is set by the multipart test helper")
        body, content_type = _multipart_body(data, files or ())
        headers.append(("content-type", content_type))
    elif json_value is not None:
        if body:
            raise ValueError("json= cannot be combined with body=")
        body = _dumps(json_value)
        if not any(name == "content-type" for name, _ in headers):
            headers.append(("content-type", "application/json"))
    return body, headers


def _multipart_body(data: FormInput | None, files: FileInput) -> tuple[bytes, str]:
    """Encode ordered form values and files as one test-only multipart body."""
    boundary = f"hinoki-test-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for name, value in _form_items(data):
        _check_disposition_value(name)
        chunks.extend(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{_quote_disposition(name)}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            )
        )
    for name, filename, content, content_type in files:
        _check_disposition_value(name)
        _check_disposition_value(filename)
        if "\r" in content_type or "\n" in content_type:
            raise ValueError("multipart content types cannot contain line breaks")
        chunks.extend(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{_quote_disposition(name)}"; '
                f'filename="{_quote_disposition(filename)}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _form_items(data: FormInput | None) -> Sequence[tuple[str, str]]:
    """Return form fields in caller order, retaining repeated names."""
    if data is None:
        return ()
    return list(data.items()) if isinstance(data, Mapping) else data


ASCII_SPACE = 0x20
ASCII_DELETE = 0x7F


def _check_disposition_value(value: str) -> None:
    if any(
        ord(character) < ASCII_SPACE or ord(character) == ASCII_DELETE
        for character in value
    ):
        raise ValueError("multipart names and filenames cannot contain controls")


def _quote_disposition(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _header_items(headers: HeaderInput | None) -> list[tuple[str, str]]:
    if headers is None:
        return []
    items = headers.items() if isinstance(headers, Mapping) else headers
    return [(name.lower(), value) for name, value in items]


class SyncStreamReply:
    """``StreamReply`` for ``SyncClient``: the pieces are read on the calling
    thread while the application runs on the client's loop."""

    def __init__(self, reply: StreamReply, owner: SyncClient) -> None:
        self.status = reply.status
        self.headers = reply.headers
        self._reply = reply
        self._owner = owner

    def chunks(self) -> Iterator[bytes]:
        pieces = self._reply.chunks()

        async def step() -> bytes | None:
            try:
                return await anext(pieces)
            except StopAsyncIteration:
                return None

        while (piece := self._owner._call(step())) is not None:
            yield piece

    def read(self, max_bytes: int | None = MAX_BYTES) -> bytes:
        return self._owner._call(self._reply.read(max_bytes))


class SyncClient:
    """The same client for tests written without ``async``: the application runs
    on its own event loop in a background thread and every call blocks until the
    answer arrives (at most ``timeout`` seconds).

        with SyncClient(create_app()) as client:
            assert client.get("/health").status == 200

    Use it only inside the ``with`` block. Sync model calls (``Model.get``,
    ``create``) work inside the application's sync handlers, not in the test
    body itself: seed and inspect data with an async fixture or your own
    connection."""

    def __init__(
        self, app: Hinoki, timeout: float = 60.0, *, host: str | None = None
    ) -> None:
        self.inner = ASGIClient(app, host=host)
        self.timeout = timeout
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="hinoki-test-loop", daemon=True
        )
        self.state = "new"  # new -> open -> closed

    def __enter__(self) -> SyncClient:
        if self.state != "new":
            raise RuntimeError("a SyncClient can be entered once")
        self.thread.start()
        try:
            start = self.inner.__aenter__()
            asyncio.run_coroutine_threadsafe(start, self.loop).result(self.timeout)
        except BaseException:
            self.stop()
            raise
        self.state = "open"
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self.state != "open":
            return  # never entered, or already closed: nothing to stop twice
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.inner.__aexit__(kind, error, trace), self.loop
            )
            future.result(self.timeout)
        finally:
            self.stop()

    def stop(self) -> None:
        self.state = "closed"
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if not self.thread.is_alive():
            self.loop.close()

    def request(self, method: str, path: str, **options: Any) -> Response:
        return self._call(self.inner.request(method, path, **options))

    def _call(self, work: Coroutine[Any, Any, T]) -> T:
        if self.state != "open":
            work.close()
            raise RuntimeError("use the SyncClient inside its `with` block")
        return asyncio.run_coroutine_threadsafe(work, self.loop).result(self.timeout)

    @contextmanager
    def stream(
        self, method: str, path: str, **options: Any
    ) -> Iterator[SyncStreamReply]:
        """``ASGIClient.stream`` for tests without ``async``:

        with client.stream("GET", "/events") as reply:
            first = next(reply.chunks())
        """
        opened = self.inner.stream(method, path, **options)
        reply = self._call(opened.__aenter__())
        try:
            yield SyncStreamReply(reply, self)
        finally:
            self._call(opened.__aexit__(None, None, None))

    def get(self, path: str, **options: Any) -> Response:
        return self.request("GET", path, **options)

    def post(self, path: str, **options: Any) -> Response:
        return self.request("POST", path, **options)

    def put(self, path: str, **options: Any) -> Response:
        return self.request("PUT", path, **options)

    def patch(self, path: str, **options: Any) -> Response:
        return self.request("PATCH", path, **options)

    def delete(self, path: str, **options: Any) -> Response:
        return self.request("DELETE", path, **options)

    def head(self, path: str, **options: Any) -> Response:
        return self.request("HEAD", path, **options)

    def options(self, path: str, **options: Any) -> Response:
        return self.request("OPTIONS", path, **options)

    def query(self, path: str, **options: Any) -> Response:
        return self.request("QUERY", path, **options)
