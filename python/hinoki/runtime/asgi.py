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

from hinoki import context
from hinoki.context import request_id
from hinoki.db import Database
from hinoki.docsui import CSP, Page
from hinoki.docsui import pages as docs_pages
from hinoki.errors import RegistrationError, ShutdownError, StartupError
from hinoki.modules import ModuleContext
from hinoki.orm import database_var
from hinoki.renderers import Renderer, negotiate
from hinoki.reply import Reply
from hinoki.request import Request
from hinoki.routing import MethodNotAllowed
from hinoki.runtime.binding import BODY_METHODS, Binding
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor
from hinoki.runtime.lifespan import Lifespan
from hinoki.runtime.requests import (
    Scope,
    authorize,
    build_request,
    decode_params,
    header_map,
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
from hinoki.settings import FrameworkSettings

if TYPE_CHECKING:
    from hinoki.app import HinokiAPI

log = logging.getLogger("hinoki.runtime")


def docs_response(page: Page) -> Response:
    """Answer for a documentation page or vendored asset."""
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", page.content_type.encode()),
        (b"x-content-type-options", b"nosniff"),
    ]
    if page.content_type.startswith("text/html"):
        headers.append((b"content-security-policy", CSP.encode()))
    cache = b"public, max-age=86400" if page.cacheable else b"no-cache"
    headers.append((b"cache-control", cache))
    return Response(200, page.body, headers)


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's runtime state."""

    def __init__(self, app: HinokiAPI) -> None:
        self.app = app
        self.bindings = app.bindings
        self.document = self._document(app)
        self.docs = docs_pages(app.openapi_url, app.docs_url, app.redoc_url)
        for path in self.docs:
            if app.lookup("GET", path) is not None:
                raise RegistrationError(
                    f"{path} is served by the documentation viewer and also "
                    "matches a route; move one of them"
                )
        self.ctx = ModuleContext(app.settings)
        self.logging = (
            app.settings.logging
            if isinstance(app.settings, FrameworkSettings)
            else None
        )
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        needs_database = any(b.route.transaction for b in self.bindings)
        self.lifespan = Lifespan(
            app.modules,
            self.ctx,
            self.executor,
            app.runtime,
            needs_database=needs_database,
        )

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
        if self.logging is not None:
            self.logging.apply()
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
        context.route_headers.set(())
        runner_token = context.sync_runner.set(self.executor.run)
        # Models use the worker's Database without it being passed around.
        db_token = database_var.set(self.ctx.find(Database))
        try:
            response = await self._respond(scope, receive, rid)
        finally:
            database_var.reset(db_token)
            context.sync_runner.reset(runner_token)
            request_id.reset(token)
        present = {name for name, _ in response.headers}
        extra = [
            (name.encode(), value.encode())
            for name, value in context.route_headers.get()
            if name.encode() not in present
        ]
        headers = [*response.headers, *extra, (b"x-request-id", rid.encode())]
        if response.status not in BODYLESS:
            headers.append((b"content-length", str(len(response.body)).encode()))
        start = {"type": "http.response.start", "status": response.status}
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": response.body})

    async def _respond(  # noqa: PLR0911 - one exit per failure class
        self, scope: Scope, receive: Any, rid: str
    ) -> Response:
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                return await self._serve(scope, receive)
        except TimeoutError as exc:
            if limit.expired():
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            return await error_response(exc, rid, self.app.errors)
        except ExecutorSaturated:  # a plain-def hook found the pool full
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        except Exception as exc:
            return await error_response(exc, rid, self.app.errors)

    async def _serve(self, scope: Scope, receive: Any) -> Response:
        """Ready check, routing, authentication, then (only if the action takes
        a body) reading and validating it, then the handler."""
        if not self.lifespan.ready:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER)
        if self.document is not None and self._is_document_request(scope):
            return Response.json(HTTPStatus.OK, self.document)
        page = self.docs.get(scope["path"]) if scope["method"] == "GET" else None
        if page is not None:
            return docs_response(page)
        binding, path_params = self._route(scope)
        owner = binding.route.owner
        context.route_headers.set(getattr(owner, "response_headers", ()))
        headers = header_map(scope)
        principal = await authorize(scope, binding, headers)
        renderer = negotiate(headers.get("accept", ""), binding.route.renderers)
        if renderer is None:
            wanted = ", ".join(r.media_type for r in binding.route.renderers)
            raise HTTPError(
                HTTPStatus.NOT_ACCEPTABLE, "not_acceptable", available=wanted
            )
        takes_body = binding.route.method in BODY_METHODS
        declared = headers.get("content-length", "")
        limit = self.app.runtime.max_body_bytes
        if (
            takes_body
            and declared.isascii()
            and declared.isdigit()
            and int(declared) > limit  # refuse before reading anything
        ):
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        raw_body = (
            await read_body(receive, self.app.runtime.max_body_bytes)
            if takes_body
            else b""
        )
        request, data = build_request(
            scope, binding, path_params, headers, raw_body, principal
        )
        return await self._invoke(binding, request, renderer, binding.collect(data))

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

    async def _invoke(
        self,
        binding: Binding,
        request: Request,
        renderer: Renderer,
        kwargs: dict[str, object],
    ) -> Response:
        """Run the handler; with ``transaction=True`` inside a transaction that
        commits only after the output is encoded, and rolls back on any error
        before it becomes an HTTP response."""
        if not binding.route.transaction:
            return await self._call(binding, request, renderer, kwargs)
        async with self.ctx.get(Database).transaction():
            return await self._call(binding, request, renderer, kwargs)

    async def _call(
        self,
        binding: Binding,
        request: Request,
        renderer: Renderer,
        kwargs: dict[str, object],
    ) -> Response:
        route = binding.route
        owner = (route.owner(),) if route.owner is not None else ()
        args = (*owner, request) if binding.takes_request else owner
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
        if isinstance(result, Reply):
            return Response(result.status, result.body, result.headers)
        if result is not None and route.status not in BODYLESS:
            shown = result if renderer.consumes_objects else encode(result)
            body = renderer.render(shown, request)
            many = len(route.renderers) > 1
            return Response.rendered(route.status, body, renderer.media_type, many)
        no_content = route.status == HTTPStatus.OK
        return Response(HTTPStatus.NO_CONTENT if no_content else route.status)
