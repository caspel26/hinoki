"""The per-request pipeline shared by every transport.

``Pipeline`` owns one worker's request state: the policies, the routing and
binding tables, the executor and the module lifespan. A transport (today the
ASGI adapter) hands it a scope-shaped request and a body source and writes back
the ``Response`` it returns, with every header already assembled, so error
translation, timeouts, request ids, context variables, transaction ordering and
header policy exist once. The handlers and this pipeline run in Python.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import itertools
import logging
import secrets
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol, cast

from hinoki import context, core
from hinoki.context import request_id
from hinoki.db import Database
from hinoki.docsui import CSP, Page
from hinoki.docsui import pages as docs_pages
from hinoki.errors import RegistrationError
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
from hinoki.runtime.requests import Scope, authorize, build_request, decode_params
from hinoki.runtime.responses import (
    BODYLESS,
    RETRY_LATER,
    HTTPError,
    Response,
    error_response,
)
from hinoki.runtime.security import headers_for, https_redirect
from hinoki.schema import encode
from hinoki.settings import AppSettings, SecuritySettings

if TYPE_CHECKING:
    from hinoki.app import Hinoki

log = logging.getLogger("hinoki.runtime")

# A route the transport matched: the action number and the raw path parameters.
MatchedRoute = tuple[int, dict[str, str]]


class BodySource(Protocol):
    """Where a request body comes from; the transport supplies it."""

    async def read(self, limit: int) -> bytes:
        """The whole body, refusing anything above ``limit`` bytes."""
        ...


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


class Pipeline:
    """One pipeline per worker process; it owns that worker's runtime state."""

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
        # Fixed for the worker's life: computed once, not for every request.
        self.request_id_name = self.request_id_header.encode("ascii")
        self.request_ids = itertools.count()
        self.request_id_prefix = secrets.token_hex(8)
        self.policy = {
            secure: headers_for(self.security, "https" if secure else "")
            for secure in (True, False)
        }
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

    async def handle(
        self,
        scope: Scope,
        source: BodySource,
        rid: str | None = None,
        route: MatchedRoute | None = None,
    ) -> Response:
        """Answer one request. The returned response carries its final headers
        (policy, route, request id, CORS, content-length) and its full body; a
        transport only drops the body for HEAD and writes it out. A transport
        that already chose the request id passes it as ``rid``, and one that
        already matched the route passes ``route`` (the action number and the
        raw, still percent-encoded path parameters), so it is not looked up again."""
        scope, trusted = effective_scope(scope, self.trusted_proxies)
        rid = rid or self._request_id(scope, trusted)
        response = await self._in_request_context(
            rid, self._respond(scope, source, rid, route)
        )
        return self.with_headers(response, scope, rid)

    async def handle_native(
        self, scope: Scope, binding: Binding, rid: str, accept: str
    ) -> Response:
        """Run an eligible handler without routing or request binding."""
        return await self._in_request_context(
            rid, self._respond_native(scope, binding, rid, accept)
        )

    async def _in_request_context(
        self, rid: str, response: Awaitable[Response]
    ) -> Response:
        token = request_id.set(rid)
        context.route_headers.set(())
        runner_token = context.sync_runner.set(self.executor.run)
        db_token = database_var.set(self.ctx.find(Database))
        try:
            return await response
        finally:
            database_var.reset(db_token)
            context.sync_runner.reset(runner_token)
            request_id.reset(token)

    async def _respond_native(
        self, scope: Scope, binding: Binding, rid: str, accept: str
    ) -> Response:
        response: Response
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                renderer = negotiate(accept, binding.route.renderers)
                if renderer is None:
                    wanted = ", ".join(
                        item.media_type for item in binding.route.renderers
                    )
                    raise HTTPError(
                        HTTPStatus.NOT_ACCEPTABLE,
                        "not_acceptable",
                        available=wanted,
                    )
                parse_query(scope.get("query_string", b""))
                response = await self._call_native(binding)
        except TimeoutError as exc:
            if limit.expired():
                response = HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            else:
                response = await error_response(exc, rid, self.app.errors, self.debug)
        except ExecutorSaturated:
            response = HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        except Exception as exc:
            response = await error_response(exc, rid, self.app.errors, self.debug)
        return response

    def _request_id(self, scope: Scope, trusted: bool) -> str:
        """The trusted inbound request id when configured and valid, else a new
        one."""
        incoming = None
        if trusted and self.trust_request_id:
            headers = scope.get("headers", [])
            incoming = inbound_request_id(headers, self.request_id_header)
            if incoming is None and any(
                name.lower() == self.request_id_name for name, _ in headers
            ):
                log.warning("ignored invalid request ID from trusted proxy")
        return incoming or self._new_request_id()

    def _new_request_id(self) -> str:
        """32 hex characters, unique within the process (random per-worker
        prefix and a counter), without a system call per request."""
        return f"{self.request_id_prefix}{next(self.request_ids):016x}"

    def with_headers(self, response: Response, scope: Scope, rid: str) -> Response:
        """Replace the answer's headers with the final ones."""
        headers, has_allow_origin = core.response_headers(
            response.headers,
            self.policy[scope.get("scheme", "") == "https"],
            context.route_headers.get(),
            self.request_id_name,
            rid.encode("ascii"),
            ALLOW_ORIGIN,
        )
        if self.cors is not None and not has_allow_origin:
            # An answer that sets its own CORS headers keeps them.
            headers.extend(self.cors.headers(origin_of(scope)))
        if response.status not in BODYLESS:
            headers.append((b"content-length", str(len(response.body)).encode()))
        # A new object: the answer may belong to a reused exception instance.
        return Response(response.status, response.body, headers)

    async def _respond(  # noqa: PLR0911 - one exit per failure class
        self, scope: Scope, source: BodySource, rid: str, route: MatchedRoute | None
    ) -> Response:
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                return await self._serve(scope, source, route)
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

    async def _serve(
        self, scope: Scope, source: BodySource, route: MatchedRoute | None
    ) -> Response:
        """Ready check, host and preflight policies, routing, authentication,
        then (only if the action takes a body) reading and validating it, then
        the handler."""
        if not self.lifespan.ready:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER)
        headers, header_total = core.request_headers(scope.get("headers", []))
        self._check_request_sizes(scope, header_total)
        early = await self._early_response(scope, headers)
        if early is not None:
            return early
        binding, path_params = self._resolve_request(scope, route)
        return await self._dispatch_request(
            scope, source, binding, path_params, headers
        )

    async def _early_response(  # noqa: PLR0911 - direct response per early policy
        self, scope: Scope, headers: dict[str, str]
    ) -> Response | None:
        """Apply request policies and serve generated documentation early."""
        if (
            self.hosts is not None
            or self.cors is not None
            or self.security.redirect_https
        ):
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
        self, scope: Scope, route: MatchedRoute | None
    ) -> tuple[Binding, dict[str, str]]:
        """Match a route (unless the transport did) and establish its
        response-header context."""
        if route is None:
            binding, path_params = self._route(scope)
        else:
            binding, path_params = self.bindings[route[0]], decode_params(route[1])
        owner = binding.route.owner
        context.route_headers.set(getattr(owner, "response_headers", ()))
        return binding, path_params

    async def _dispatch_request(
        self,
        scope: Scope,
        source: BodySource,
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
            await self._body(source, headers.get("content-length", ""))
            if binding.route.method in BODY_METHODS
            else b""
        )
        request, data = build_request(
            scope, binding, path_params, headers, raw_body, principal, query
        )
        return await self._invoke(binding, request, renderer, binding.collect(data))

    async def _body(self, source: BodySource, declared: str) -> bytes:
        """The request body, refused before reading anything when the declared
        length is above the limit."""
        limit = self.app.runtime.max_body_bytes
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        return await source.read(limit)

    def _check_request_sizes(self, scope: Scope, header_total: int) -> None:
        """Raise for oversized query/header bytes before parsing or routing."""
        if len(scope.get("query_string", b"")) > self.app.runtime.max_query_bytes:
            raise HTTPError(HTTPStatus.REQUEST_URI_TOO_LONG, "uri_too_long")
        if header_total > self.app.runtime.max_header_bytes:
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
        result = await self._handler_result(binding, request, kwargs)
        if isinstance(result, Reply):
            return Response(result.status, result.body, result.headers)
        if result is not None and route.status not in BODYLESS:
            shown = result if renderer.consumes_objects else encode(result)
            body = renderer.render(shown, request)
            many = len(route.renderers) > 1
            return Response.rendered(route.status, body, renderer.media_type, many)
        no_content = route.status == HTTPStatus.OK
        return Response(HTTPStatus.NO_CONTENT if no_content else route.status)

    async def _call_native(self, binding: Binding) -> Response:
        """Run a selected no-Request JSON handler and encode through Rust."""
        route = binding.route
        result = await self._handler_result(binding, None, {})
        if isinstance(result, Reply):
            return Response(result.status, result.body, result.headers)
        if result is not None and route.status not in BODYLESS:
            body = core.encode_json(result, None, False, None)
            return Response.rendered(route.status, body, "application/json", False)
        if route.status == HTTPStatus.OK:
            return Response(HTTPStatus.NO_CONTENT)
        return Response(route.status)

    async def _handler_result(
        self,
        binding: Binding,
        request: Request | None,
        kwargs: dict[str, object],
    ) -> object:
        """Call an async handler in its task or a sync handler in the executor."""
        route = binding.route
        owner = (route.owner(),) if route.owner is not None else ()
        if binding.takes_request:
            if request is None:
                raise RuntimeError("a handler requiring Request has no request")
            args = (*owner, request)
        else:
            args = owner
        try:
            if binding.is_async:
                handler = cast(Callable[..., Awaitable[object]], route.handler)
                return await handler(*args, **kwargs)
            result = await self.executor.run(route.handler, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except ExecutorSaturated:
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ) from None
