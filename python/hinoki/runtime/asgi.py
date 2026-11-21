"""ASGI adapter: the lifespan protocol and the HTTP message exchange for one
worker.

Everything that decides an answer (routing, binding, policies, handlers, error
mapping) lives in ``Pipeline``. The ASGI boundary crosses Python objects; this
is not a native-only path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hinoki.errors import ShutdownError, StartupError
from hinoki.http.stream import EventStream, Sourced
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.requests import Scope, read_body
from hinoki.runtime.responses import Response, StreamHandle
from hinoki.runtime.streaming import (
    Pace,
    discard,
    keepalive_of,
    pump_asgi,
    reader_for,
)
from hinoki.settings import ANY, AppSettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")

PATHSEND = "http.response.pathsend"

# A lifespan scope key set by in-process drivers (the test clients, the MCP
# scenario runner): no network Host header reaches them, so the startup warning
# about ``allowed_hosts = *`` would only be noise there.
IN_PROCESS = "hinoki.in_process"

ANY_HOST_WARNING = (
    "allowed_hosts is '*' and debug is off: every Host header is accepted, so "
    "links, redirects and cache keys built from it (a password-reset link, for "
    "example) can name a host the client chose (Host header injection); name "
    'the served hosts, e.g. allowed_hosts=("app.example.com",) or '
    '(".example.com",) for a domain and its subdomains'
)


def accepts_any_host(settings: object) -> bool:
    """Whether a deployment with these settings answers to every Host header
    while ``debug`` is off."""
    return (
        isinstance(settings, AppSettings)
        and not settings.debug
        and settings.allowed_hosts == (ANY,)
    )


class ASGIBody:
    """The request body read through the ASGI ``receive`` callable."""

    __slots__ = ("receive",)

    def __init__(self, receive: Any) -> None:
        self.receive = receive

    async def read(self, limit: int) -> bytes:
        return await read_body(self.receive, limit)


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's pipeline."""

    def __init__(self, app: Hinoki) -> None:
        self.pipeline = Pipeline(app)
        self.lifespan = self.pipeline.lifespan

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send, served=not scope.get(IN_PROCESS))
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")

    async def _lifespan(self, receive: Any, send: Any, *, served: bool) -> None:
        finished = False
        while not finished:
            kind = (await receive())["type"]
            if kind == "lifespan.startup":
                finished = await self._startup(send, served=served)
            elif kind == "lifespan.shutdown":
                self.pipeline.streams.stop_events()  # SSE streams end cleanly
                await self._shutdown(send)
                finished = True

    async def _startup(self, send: Any, *, served: bool = True) -> bool:
        """Answer startup; return True when the protocol loop must end."""
        if self.pipeline.logging is not None:
            self.pipeline.logging.apply()
        if self.pipeline.debug:
            log.warning("debug is on: error responses include tracebacks")
        if served and accepts_any_host(self.pipeline.app.settings):
            log.warning(ANY_HOST_WARNING)
        try:
            await self.lifespan.startup()
        except StartupError as exc:
            log.error("startup failed", exc_info=exc.cause)
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return True
        await send({"type": "lifespan.startup.complete"})
        return False

    async def _shutdown(self, send: Any) -> None:
        try:
            await self.lifespan.shutdown()
        except ShutdownError as exc:
            log.error("shutdown errors", exc_info=exc)
            await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
        else:
            await send({"type": "lifespan.shutdown.complete"})

    async def _http(self, scope: Scope, receive: Any, send: Any) -> None:
        response = await self.pipeline.handle(scope, ASGIBody(receive))
        start = {"type": "http.response.start", "status": response.status}
        if response.stream is not None:
            await self._stream(scope, receive, send, response, start)
            return
        await send({**start, "headers": response.headers})
        body = b"" if scope["method"] == "HEAD" else response.body
        await send({"type": "http.response.body", "body": body})

    async def _stream(
        self,
        scope: Scope,
        receive: Any,
        send: Any,
        response: Response,
        start: dict[str, Any],
    ) -> None:
        """Send a streamed answer through the ASGI callables (Python objects for
        every piece). The place it holds is given back once the head is sent."""
        handle = response.stream
        if handle is None:
            raise RuntimeError("not a streamed response")
        pipeline = self.pipeline
        try:
            await send({**start, "headers": response.headers})
            if scope["method"] == "HEAD":
                await send({"type": "http.response.body", "body": b""})
                await discard(handle, pipeline.executor)
                return
            handle.release()
            await self._pump(scope, receive, send, handle)
        finally:
            handle.release()

    async def _pump(
        self, scope: Scope, receive: Any, send: Any, handle: StreamHandle
    ) -> None:
        pipeline = self.pipeline
        answer = handle.answer
        if handle.location is not None and PATHSEND in scope.get("extensions", {}):
            await send({"type": PATHSEND, "path": handle.location[0]})
            return
        runtime = pipeline.app.runtime
        events = isinstance(answer, EventStream)
        await pump_asgi(
            send,
            receive,
            reader_for(handle, pipeline.executor),
            Pace(
                answer.preamble if isinstance(answer, Sourced) else b"",
                None if events else runtime.idle_timeout,
                keepalive_of(answer, runtime.sse_keepalive),
                events,
            ),
            pipeline.streams,
        )
