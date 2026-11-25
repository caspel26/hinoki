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
from hinoki.runtime.asgisocket import (
    ASGITransport,
    accept,
    connected,
    origin_allowed,
    refuse,
)
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.proxy import effective_scope
from hinoki.runtime.requests import Scope, decode_params, header_map, read_body
from hinoki.runtime.responses import HTTPError, Response, StreamHandle
from hinoki.runtime.streaming import (
    Pace,
    declared_length,
    discard,
    keepalive_of,
    pump_asgi,
    reader_for,
)
from hinoki.websocket import WebSocketRoute

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

HOST_CHECK_REQUIRED = (
    "allowed_hosts is '*' and debug is off, but this app serves {what}, whose "
    "same-origin check trusts the Host header the client sent (DNS rebinding "
    "passes it); name the served hosts, e.g. "
    'allowed_hosts=("app.example.com",)'
)


def host_trusting_feature(app: Any) -> str | None:
    """What the app serves that decides same-origin from the Host header."""
    if app.has_admin:
        return "the admin"
    return "WebSocket routes" if app.websockets else None


def accepts_any_host(pipeline: Pipeline) -> bool:
    """Whether the app answers to every Host header while ``debug`` is off:
    no host policy applies, which includes an app built without settings."""
    return pipeline.hosts is None and not pipeline.debug


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
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")

    async def _websocket(self, scope: Scope, receive: Any, send: Any) -> None:
        # ASGI opens with ``websocket.connect``: it is read before any answer,
        # a refusal included, and a client gone before it ends the call.
        if not await connected(receive):
            return
        pipeline = self.pipeline
        scope, _ = effective_scope(scope, pipeline.trusted_proxies)
        resolved = self._websocket_target(scope)
        if resolved is None:
            await refuse(send)
            return
        path, route, params = resolved
        cors = pipeline.cors.origins if pipeline.cors is not None else frozenset()
        if not origin_allowed(route, scope, cors):
            await refuse(send)
            return
        rid = pipeline.new_request_id()
        admitted = await pipeline.admit_websocket(rid, route, scope, params)
        if isinstance(admitted, Response):
            await refuse(send)
            return
        await accept(send, admitted.subprotocol)
        runtime = pipeline.app.runtime
        transport = ASGITransport(
            receive, send, route.max_message_bytes or runtime.ws_max_message_bytes
        )
        await pipeline.run_websocket(rid, route, transport, admitted, path)

    def _websocket_target(
        self, scope: Scope
    ) -> tuple[str, WebSocketRoute, dict[str, str]] | None:
        """The route of a handshake, matched as the HTTP pipeline matches: on
        the raw path when the server gives one (ASGI ``path`` is already
        decoded, so decoding it again would turn ``%252f`` into ``/``), after
        the Host policy. ``None`` refuses the handshake."""
        pipeline = self.pipeline
        if pipeline.hosts is not None:
            host = header_map(scope).get("host", "")
            if not pipeline.hosts.allows(host):
                return None
        raw_path = scope.get("raw_path")
        try:
            path = scope["path"] if raw_path is None else pipeline.raw_target(raw_path)
            found = pipeline.app.lookup_websocket(path)
            if found is None:
                return None
            route, params = found
            if raw_path is not None:
                params = decode_params(params)
        except HTTPError:
            return None
        return path, route, params

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
        if served and accepts_any_host(self.pipeline):
            what = host_trusting_feature(self.pipeline.app)
            if what is not None:
                message = HOST_CHECK_REQUIRED.format(what=what)
                log.error(message)
                await send({"type": "lifespan.startup.failed", "message": message})
                return True
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
        every piece). The place it holds is given back when the body ends."""
        handle = response.stream
        if handle is None:
            raise RuntimeError("not a streamed response")  # pragma: no cover
        pipeline = self.pipeline
        try:
            await send({**start, "headers": response.headers})
            if scope["method"] == "HEAD":
                await send({"type": "http.response.body", "body": b""})
                await discard(handle, pipeline.executor)
                return
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
                declared_length(handle),
            ),
            pipeline.streams,
        )
