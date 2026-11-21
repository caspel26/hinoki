"""A WSGI adapter for an application: ``wsgi_app = WSGIAdapter(app)``.

WSGI has no lifespan and no event loop, so the adapter owns both: on the first
request it starts one background event loop thread, runs the application's
startup there (modules, database pool) and drives each request through the same
ASGI callable that ``hinoki serve`` uses. Async handlers run on that loop, sync
handlers on the bounded executor, exactly as under ASGI. Responses are buffered
(Hinoki builds the whole body before sending), so streaming is not offered.

Startup is lazy so a pre-forking server can import the module in its master
process and let each worker start its own loop. Shutdown runs from ``close()``
or, failing that, at interpreter exit. This is a compatibility path: a server
thread blocks for the duration of each request, so ASGI remains the intended
way to serve Hinoki.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import Any


class WSGIStartupError(RuntimeError):
    """The application did not start; every request answers 503."""


Environ = dict[str, Any]
StartResponse = Callable[..., Any]


class WSGIAdapter:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._inbox: asyncio.Queue[dict[str, Any]] | None = None
        self._events: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan: asyncio.Task[None] | None = None
        self._failure: str | None = None

    def start(self) -> None:
        """Start the loop and the application's startup; safe to call twice."""
        with self._lock:
            self._refuse_streams()
            if self._loop is not None:
                if self._failure is not None:
                    raise WSGIStartupError(self._failure)
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="hinoki-wsgi-loop", daemon=True
            )
            thread.start()
            self._loop, self._thread = loop, thread
            try:
                asyncio.run_coroutine_threadsafe(self._startup(), loop).result()
            except Exception as exc:
                self._failure = str(exc) or type(exc).__name__
                raise
            atexit.register(self.close)

    def _refuse_streams(self) -> None:
        """Stream, EventStream and File answers need a transport that can send a
        body as it is produced; WSGI here collects the whole answer."""
        names = [
            binding.route.name
            for binding in getattr(self.app, "bindings", ())
            if binding.streams
        ]
        if names:
            raise WSGIStartupError(
                "WSGIAdapter cannot serve streamed responses (Stream, "
                f"EventStream, File); serve with ASGI or Kiso: {', '.join(names)}"
            )

    async def _startup(self) -> None:
        self._inbox = asyncio.Queue()
        self._events = asyncio.Queue()
        inbox, events = self._inbox, self._events

        async def send(message: dict[str, Any]) -> None:
            await events.put(message)

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        self._lifespan = asyncio.create_task(self.app(scope, inbox.get, send))
        await inbox.put({"type": "lifespan.startup"})
        reply = await events.get()
        if reply["type"] != "lifespan.startup.complete":
            raise WSGIStartupError(str(reply.get("message", "startup failed")))

    def close(self) -> None:
        """Run the application's shutdown and stop the loop; idempotent."""
        with self._lock:
            loop, self._loop = self._loop, None
        if loop is None or self._failure is not None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
        atexit.unregister(self.close)

    async def _shutdown(self) -> None:
        if self._inbox is None or self._events is None or self._lifespan is None:
            return
        await self._inbox.put({"type": "lifespan.shutdown"})
        await self._events.get()
        await self._lifespan

    def __call__(
        self, environ: Environ, start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            self.start()
        except Exception:
            start_response(_status(503), [("content-type", "application/json")])
            return [b'{"error":"not_ready","message":"the application did not start"}']
        loop = self._loop
        if loop is None:
            raise WSGIStartupError("the adapter is closed")
        try:
            body = _read(environ, self.app.runtime.max_body_bytes)
        except BadLength:
            start_response(_status(400), [("content-type", "application/json")])
            return [
                b'{"error":"bad_content_length","message":"invalid Content-Length"}'
            ]
        scope = _scope(environ)
        future = asyncio.run_coroutine_threadsafe(self._request(scope, body), loop)
        status, headers, payload = future.result()
        start_response(_status(status), headers)
        return [payload]

    async def _request(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if sent:
                await asyncio.Event().wait()  # no disconnect is ever reported
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        status = 500
        headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (name.decode("latin-1"), value.decode("latin-1"))
                    for name, value in message["headers"]
                ]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        return status, headers, b"".join(chunks)


def _status(code: int) -> str:
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


class BadLength(ValueError):
    """Content-Length is not a non-negative integer."""


def _read(environ: Environ, limit: int) -> bytes:
    """The request body, read up to one byte past ``limit`` so the application
    answers 413 instead of the adapter buffering an unbounded upload."""
    stream = environ["wsgi.input"]
    length = environ.get("CONTENT_LENGTH")
    chunked = "chunked" in environ.get("HTTP_TRANSFER_ENCODING", "").lower()
    if length:
        if not length.isascii() or not length.isdigit():
            raise BadLength(length)
        return bytes(stream.read(min(int(length), limit + 1)))
    if chunked:
        return bytes(stream.read(limit + 1))
    return b""


def _scope(environ: Environ) -> dict[str, Any]:
    headers = [
        (key[5:].replace("_", "-").lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in environ.items()
        if key.startswith("HTTP_")
    ]
    headers.extend(
        (key.replace("_", "-").lower().encode(), environ[key].encode())
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH")
        if environ.get(key)
    )
    # PATH_INFO is already percent-decoded and latin-1 (PEP 3333); no raw_path, so
    # the application does not decode it a second time.
    path = (
        (environ.get("PATH_INFO", "") or "/")
        .encode("latin-1")
        .decode("utf-8", "replace")
    )
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": environ.get("SERVER_PROTOCOL", "HTTP/1.1").removeprefix(
            "HTTP/"
        ),
        "method": environ["REQUEST_METHOD"],
        "scheme": environ.get("wsgi.url_scheme", "http"),
        "path": path,
        "query_string": environ.get("QUERY_STRING", "").encode("latin-1"),
        "root_path": environ.get("SCRIPT_NAME", ""),
        "headers": headers,
        "server": (
            environ.get("SERVER_NAME", ""),
            int(environ.get("SERVER_PORT") or 0),
        ),
        "client": (environ.get("REMOTE_ADDR", ""), 0),
    }
