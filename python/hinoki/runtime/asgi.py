"""ASGI adapter: the lifespan protocol and request dispatch for one worker.

Scope: lifespan, routing on the raw path, path/query/body binding, access
policies, sync/async handler execution with limits, error mapping. The ASGI
boundary crosses Python objects; this is not a native-only path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from hinoki.context import request_id
from hinoki.errors import RegistrationError, ShutdownError, StartupError
from hinoki.modules import ModuleContext
from hinoki.routing import MethodNotAllowed
from hinoki.runtime.binding import Binding
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor
from hinoki.runtime.lifespan import Lifespan
from hinoki.runtime.requests import (
    Scope,
    authorize,
    build_request,
    decode_params,
    read_body,
)
from hinoki.runtime.responses import (
    BODYLESS,
    RETRY_LATER,
    HTTPError,
    Response,
    error_response,
)
from hinoki.schema import encode

if TYPE_CHECKING:
    from hinoki.app import HinokiAPI

log = logging.getLogger("hinoki.runtime")


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's runtime state."""

    def __init__(self, app: HinokiAPI) -> None:
        self.app = app
        self.bindings = app.bindings
        self.document = self._document(app)
        self.ctx = ModuleContext(app.settings)
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        self.lifespan = Lifespan(app.modules, self.ctx, self.executor, app.runtime)

    @staticmethod
    def _document(app: HinokiAPI) -> dict[str, Any] | None:
        """Build the OpenAPI document once, at startup, if it is served."""
        if app.openapi_url is None:
            return None
        if any(route.path == app.openapi_url for route in app.routes):
            raise RegistrationError(f"{app.openapi_url} is already used by a route")
        return app.openapi()

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
        headers = [*response.headers, (b"x-request-id", rid.encode())]
        if response.status not in BODYLESS:
            headers.append((b"content-length", str(len(response.body)).encode()))
        start = {"type": "http.response.start", "status": response.status}
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})

    async def _respond(self, scope: Scope, receive: Any, rid: str) -> Response:
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                return await self._serve(scope, receive)
        except TimeoutError as exc:
            if limit.expired():
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            return await error_response(exc, rid, self.app.errors)
        except Exception as exc:
            return await error_response(exc, rid, self.app.errors)

    async def _serve(self, scope: Scope, receive: Any) -> Response:
        """Ready check, routing, authentication, then (only if the action takes
        a body) reading and validating it, then the handler."""
        if not self.lifespan.ready:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER)
        if self.document is not None and self._is_document_request(scope):
            return Response.json(HTTPStatus.OK, self.document)
        binding, path_params = self._route(scope)
        principal = await authorize(scope, binding)
        raw_body = await read_body(receive) if binding.needs_body else b""
        request = build_request(scope, binding, path_params, raw_body, principal)
        return await self._invoke(binding, binding.collect(request))

    def _is_document_request(self, scope: Scope) -> bool:
        return bool(scope["method"] == "GET" and scope["path"] == self.app.openapi_url)

    def _route(self, scope: Scope) -> tuple[Binding, dict[str, str]]:
        raw_path = scope.get("raw_path")
        path = scope["path"] if raw_path is None else self._raw_target(raw_path)
        found = self.app.lookup(scope["method"], path)
        if found is None:
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found")
        if isinstance(found, MethodNotAllowed):
            allow = ", ".join(found.allowed).encode()
            raise HTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", [(b"allow", allow)]
            )
        params = found.params if raw_path is None else decode_params(found.params)
        return self.bindings[found.action], params

    @staticmethod
    def _raw_target(raw_path: bytes) -> str:
        try:
            return raw_path.split(b"?", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding") from None

    async def _invoke(self, binding: Binding, kwargs: dict[str, object]) -> Response:
        route = binding.route
        args = (route.owner(),) if route.owner is not None else ()
        try:
            if route.is_async:
                handler = cast(Callable[..., Awaitable[object]], route.handler)
                result = await handler(*args, **kwargs)
            else:
                result = await self.executor.run(route.handler, *args, **kwargs)
        except ExecutorSaturated:
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ) from None
        if result is not None and route.status not in BODYLESS:
            return Response.json(route.status, encode(result))
        no_content = route.status == HTTPStatus.OK
        return Response(HTTPStatus.NO_CONTENT if no_content else route.status)
