"""ASGI adapter: lifespan protocol plus path-parameter request dispatch.

Current scope (R3): lifespan, routing, path-parameter binding, sync/async
handler execution with limits, and error mapping. No request body, query
string, schemas or auth yet. The ASGI boundary crosses Python objects; this
is not a native-only path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from hinoki.context import request_id
from hinoki.errors import ShutdownError, StartupError
from hinoki.modules import ModuleContext
from hinoki.routing import MethodNotAllowed
from hinoki.runtime.binding import BadParameter, Binding, bind
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor
from hinoki.runtime.lifespan import Lifespan

if TYPE_CHECKING:
    from hinoki.app import HinokiAPI

log = logging.getLogger("hinoki.runtime")

MAX_BODY = 1024 * 1024
Scope = MutableMapping[str, Any]
Headers = list[tuple[bytes, bytes]]
RETRY_LATER: Headers = [(b"retry-after", b"1")]


@dataclass(slots=True)
class Response:
    status: int
    body: bytes = b""
    headers: Headers = field(default_factory=list)

    @classmethod
    def json(
        cls, status: int, payload: object, headers: Headers | None = None
    ) -> Response:
        head: Headers = [(b"content-type", b"application/json"), *(headers or [])]
        return cls(status, json.dumps(payload).encode(), head)


class HTTPError(Exception):
    """Raised inside request handling to answer with a specific response."""

    def __init__(
        self, status: int, error: str, headers: Headers | None = None, **extra: object
    ) -> None:
        super().__init__(error)
        self.response = Response.json(status, {"error": error, **extra}, headers)


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's runtime state."""

    def __init__(self, app: HinokiAPI) -> None:
        self.app = app
        self.bindings: list[Binding] = [bind(r) for r in app.routes]
        self.ctx = ModuleContext(app.settings)
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        self.lifespan = Lifespan(app.modules, self.ctx, self.executor, app.runtime)

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise RuntimeError(f"unsupported scope type {scope['type']!r}")

    async def _lifespan(self, receive: Any, send: Any) -> None:
        finished = False
        while not finished:
            kind = (await receive())["type"]
            if kind == "lifespan.startup":
                finished = await self._startup(send)
            elif kind == "lifespan.shutdown":
                await self._shutdown(send)
                finished = True

    async def _startup(self, send: Any) -> bool:
        """Answer startup; return True when the protocol loop must end."""
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
        rid = uuid.uuid4().hex
        token = request_id.set(rid)
        try:
            response = await self._respond(scope, receive, rid)
        finally:
            request_id.reset(token)
        headers = [
            *response.headers,
            (b"x-request-id", rid.encode()),
            (b"content-length", str(len(response.body)).encode()),
        ]
        start = {"type": "http.response.start", "status": response.status}
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})

    async def _respond(self, scope: Scope, receive: Any, rid: str) -> Response:
        try:
            await self._drain_body(receive)
            if not self.lifespan.ready:
                raise HTTPError(
                    HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER
                )
            binding, params = self._route(scope)
            return await self._invoke(binding, binding.convert(params))
        except HTTPError as exc:
            return exc.response
        except BadParameter as exc:
            error = HTTPError(
                HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", parameter=exc.name
            )
            return error.response
        except Exception:
            log.exception("unhandled error in request %s", rid)
            error = HTTPError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", request_id=rid
            )
            return error.response

    def _route(self, scope: Scope) -> tuple[Binding, dict[str, str]]:
        found = self.app.lookup(scope["method"], scope["path"])
        if found is None:
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found")
        if isinstance(found, MethodNotAllowed):
            allow = ", ".join(found.allowed).encode()
            raise HTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", [(b"allow", allow)]
            )
        return self.bindings[found.action], found.params

    async def _invoke(self, binding: Binding, kwargs: dict[str, object]) -> Response:
        route = binding.route
        args = (route.owner(),) if route.owner is not None else ()
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                if route.is_async:
                    handler = cast(Callable[..., Awaitable[object]], route.handler)
                    result = await handler(*args, **kwargs)
                else:
                    result = await self.executor.run(route.handler, *args, **kwargs)
        except ExecutorSaturated:
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ) from None
        except TimeoutError:
            if limit.expired():
                raise HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout") from None
            raise
        if result is not None:
            return Response.json(route.status, result)
        no_content = route.status == HTTPStatus.OK
        return Response(HTTPStatus.NO_CONTENT if no_content else route.status)

    @staticmethod
    async def _drain_body(receive: Any) -> None:
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                raise HTTPError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large"
                )
            more = message.get("more_body", False)
