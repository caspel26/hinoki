"""ASGI adapter: the lifespan protocol and request dispatch for one worker.

Scope: lifespan, routing on the raw path, path/query/body binding, access
policies, sync/async handler execution with limits, error mapping. The ASGI
boundary crosses Python objects; this is not a native-only path.
"""

from __future__ import annotations

import asyncio
import ipaddress
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
from hinoki.runtime.binding import BODY_METHODS, Binding, parse_query
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor
from hinoki.runtime.lifespan import Lifespan
from hinoki.runtime.origins import ALLOW_ORIGIN, CorsPolicy, HostPolicy
from hinoki.runtime.proxy import effective_scope, inbound_request_id
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
from hinoki.runtime.security import (
    POLICY_HEADERS,
    headers_for,
    https_redirect,
    merge_policy_headers,
)
from hinoki.schema import encode
from hinoki.settings import AppSettings, SecuritySettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

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


def origin_of(scope: Scope) -> str:
    """The request's ``Origin`` header, or an empty string. Repeated headers
    are joined as ``header_map`` joins them, so they match no single origin."""
    found = [value for name, value in scope.get("headers", ()) if name == b"origin"]
    return b", ".join(found).decode("latin-1")


class ASGIAdapter:
    """One adapter per worker process; it owns that worker's runtime state."""

    def __init__(self, app: Hinoki) -> None:
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
            app.settings.logging if isinstance(app.settings, AppSettings) else None
        )
        self.debug = isinstance(app.settings, AppSettings) and app.settings.debug
        self.hosts = HostPolicy.of(app.settings)
        self.cors = CorsPolicy.of(app.settings)
        settings = app.settings if isinstance(app.settings, AppSettings) else None
        self.security = settings.security if settings else SecuritySettings()
        self.trusted_proxies = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in (settings.server.trusted_proxies if settings else ())
        )
        self.request_id_header = (
            settings.server.request_id_header.lower() if settings else "x-request-id"
        )
        self.trust_request_id = settings.server.trust_request_id if settings else False
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        needs_database = any(b.route.transaction for b in self.bindings)
        self.lifespan = Lifespan(
            app.modules,
            self.ctx,
            self.executor,
            app.runtime,
            needs_database=needs_database,
            on_startup=app.on_startup,
            on_shutdown=app.on_shutdown,
        )

    @staticmethod
    def _document(app: Hinoki) -> dict[str, Any] | None:
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
        if self.debug:
            log.warning("debug is on: error responses include tracebacks")
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
        scope, trusted = effective_scope(scope, self.trusted_proxies)
        request_id_headers = scope.get("headers", [])
        incoming_rid = None
        if trusted and self.trust_request_id:
            incoming_rid = inbound_request_id(
                request_id_headers, self.request_id_header
            )
            if incoming_rid is None and any(
                name.lower() == self.request_id_header.encode("ascii")
                for name, _ in request_id_headers
            ):
                log.warning("ignored invalid request ID from trusted proxy")
        rid = incoming_rid or uuid.uuid4().hex
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
        merge_policy_headers(
            response, headers_for(self.security, scope.get("scheme", ""))
        )
        present = {name.lower() for name, _ in response.headers}
        extra = [
            (name.encode(), value.encode())
            for name, value in context.route_headers.get()
            if name.encode().lower() not in present
            and name.encode().lower() not in POLICY_HEADERS
        ]
        request_id_bytes = self.request_id_header.encode("ascii")
        headers = [
            (name, value)
            for name, value in [*response.headers, *extra]
            if name.lower() != request_id_bytes
        ]
        headers.append((request_id_bytes, rid.encode("ascii")))
        if self.cors is not None and ALLOW_ORIGIN not in present:
            # An answer that sets its own CORS headers keeps them.
            headers.extend(self.cors.headers(origin_of(scope)))
        if response.status not in BODYLESS:
            headers.append((b"content-length", str(len(response.body)).encode()))
        start = {"type": "http.response.start", "status": response.status}
        await send({**start, "headers": headers})
        body = b"" if scope["method"] == "HEAD" else response.body
        await send({"type": "http.response.body", "body": body})

    async def _respond(  # noqa: PLR0911 - one exit per failure class
        self, scope: Scope, receive: Any, rid: str
    ) -> Response:
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                return await self._serve(scope, receive)
        except TimeoutError as exc:
            if limit.expired():
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            return await error_response(exc, rid, self.app.errors, self.debug)
        except ExecutorSaturated:  # a plain-def hook found the pool full
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        except Exception as exc:
            return await error_response(exc, rid, self.app.errors, self.debug)

    async def _serve(self, scope: Scope, receive: Any) -> Response:
        """Ready check, host and preflight policies, routing, authentication,
        then (only if the action takes a body) reading and validating it, then
        the handler."""
        if not self.lifespan.ready:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER)
        self._check_request_sizes(scope)
        early = await self._early_response(scope)
        if early is not None:
            return early
        binding, path_params, headers = self._resolve_request(scope)
        return await self._dispatch_request(
            scope, receive, binding, path_params, headers
        )

    async def _early_response(  # noqa: PLR0911 - direct response per early policy
        self, scope: Scope
    ) -> Response | None:
        """Apply request policies and serve generated documentation early."""
        if (
            self.hosts is not None
            or self.cors is not None
            or self.security.redirect_https
        ):
            headers = header_map(scope)
            preflight = self._guard(scope["method"], headers)
            if preflight is not None:
                return preflight
            redirect = https_redirect(
                scope,
                headers,
                self.app.settings
                if isinstance(self.app.settings, AppSettings)
                else None,
            )
            if redirect is not None:
                return redirect
        if self.document is not None and self._is_document_request(scope):
            return Response.json(HTTPStatus.OK, self.document)
        page = (
            self.docs.get(scope["path"]) if scope["method"] in {"GET", "HEAD"} else None
        )
        if page is not None:
            return docs_response(page)
        return None

    def _resolve_request(
        self, scope: Scope
    ) -> tuple[Binding, dict[str, str], dict[str, str]]:
        """Match a route and establish its response-header context."""
        binding, path_params = self._route(scope)
        owner = binding.route.owner
        context.route_headers.set(getattr(owner, "response_headers", ()))
        return binding, path_params, header_map(scope)

    async def _dispatch_request(
        self,
        scope: Scope,
        receive: Any,
        binding: Binding,
        path_params: dict[str, str],
        headers: dict[str, str],
    ) -> Response:
        """Authorize, negotiate, read only required bodies, and invoke."""
        access = binding.route.access
        query = principal = None
        if access is not None:
            # Parsed once: authentication and the request share the result.
            query = parse_query(scope.get("query_string", b""))
            principal = await authorize(access, headers, query)
        renderer = negotiate(headers.get("accept", ""), binding.route.renderers)
        if renderer is None:
            wanted = ", ".join(r.media_type for r in binding.route.renderers)
            raise HTTPError(
                HTTPStatus.NOT_ACCEPTABLE, "not_acceptable", available=wanted
            )
        raw_body = (
            await self._body(receive, headers.get("content-length", ""))
            if binding.route.method in BODY_METHODS
            else b""
        )
        request, data = build_request(
            scope, binding, path_params, headers, raw_body, principal, query
        )
        return await self._invoke(binding, request, renderer, binding.collect(data))

    async def _body(self, receive: Any, declared: str) -> bytes:
        """The request body, refused before reading anything when the declared
        length is above the limit."""
        limit = self.app.runtime.max_body_bytes
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        return await read_body(receive, limit)

    def _check_request_sizes(self, scope: Scope) -> None:
        """Raise for oversized query/header bytes before parsing or routing."""
        if len(scope.get("query_string", b"")) > self.app.runtime.max_query_bytes:
            raise HTTPError(HTTPStatus.REQUEST_URI_TOO_LONG, "uri_too_long")
        header_bytes = sum(
            len(name) + len(value) + 4 for name, value in scope.get("headers", ())
        )
        if header_bytes > self.app.runtime.max_header_bytes:
            raise HTTPError(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE, "headers_too_large"
            )

    def _guard(self, method: str, headers: dict[str, str]) -> Response | None:
        """Refuse a host that is not allowed; answer a CORS preflight."""
        if self.hosts is not None:
            self.hosts.check(headers.get("host", ""))
        if self.cors is not None:
            return self.cors.answer(method, headers)
        return None

    def _is_document_request(self, scope: Scope) -> bool:
        return bool(
            scope["method"] in {"GET", "HEAD"} and scope["path"] == self.app.openapi_url
        )

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
