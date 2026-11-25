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
import contextvars
import inspect
import ipaddress
import itertools
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterator
from http import HTTPStatus
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

from hinoki import context, core
from hinoki.ai.llms import render_llms
from hinoki.auth import Caller, _LazyHeaders
from hinoki.context import request_id
from hinoki.db import Database
from hinoki.docsui import CSP, Page, llms_pages
from hinoki.docsui import pages as docs_pages
from hinoki.errors import (
    MAX_RETRY_AFTER,
    Conflict,
    ErrorDetail,
    InvalidBody,
    RegistrationError,
    TooManyRequests,
    ValidationError,
)
from hinoki.events import RateLimited, events_var, listener
from hinoki.http.middleware import MiddlewareRequest
from hinoki.http.request import Request, media_type
from hinoki.http.response import Response as HandlerResponse
from hinoki.http.response import encode_header
from hinoki.http.security import same_site_origin
from hinoki.http.stream import File, Streamed
from hinoki.mail import Mailer, mailer_var
from hinoki.modules import ModuleContext
from hinoki.orm.context import database_var
from hinoki.orm.links import check_resolved
from hinoki.orm.model import Model
from hinoki.orm.projection import _ProjectedItem, _ProjectedList, _ProjectedPage
from hinoki.pagination import CursorPage
from hinoki.pagination import Page as ResultPage
from hinoki.parsers import JSONParser, Parser, choose, is_native_parser
from hinoki.ratelimit import RateLimit, RateLimitContext
from hinoki.renderers import (
    JSONRenderer,
    Renderer,
    is_default_json_renderer,
    negotiate,
)
from hinoki.routing import Found, MethodNotAllowed, Route
from hinoki.runtime.binding import (
    BODY_METHODS,
    Binding,
    Param,
    RequestData,
    parse_query,
)
from hinoki.runtime.deprecation import Reminders
from hinoki.runtime.executor import ExecutorSaturated, SyncExecutor
from hinoki.runtime.inline import RequestDeadline, run_inline
from hinoki.runtime.lifespan import Lifespan
from hinoki.runtime.origins import ALLOW_ORIGIN, CorsPolicy, HostPolicy
from hinoki.runtime.proxy import (
    build_proxy_policy,
    effective_client,
    effective_scope,
    inbound_request_id_checked,
)
from hinoki.runtime.requests import (
    Scope,
    authorize,
    build_request,
    decode_params,
    plain_data,
)
from hinoki.runtime.responsecheck import ResponseContractError
from hinoki.runtime.responses import (
    BODYLESS,
    RETRY_LATER,
    HTTPError,
    Response,
    StreamHandle,
    error_response,
    error_response_now,
)
from hinoki.runtime.security import headers_for, https_redirect
from hinoki.runtime.streaming import (
    Streams,
    declared_length,
    discard,
    discard_answer,
)
from hinoki.runtime.websockets import Admitted
from hinoki.runtime.websockets import admit as admit_handshake
from hinoki.runtime.websockets import needs_session as websocket_needs_session
from hinoki.runtime.websockets import run as run_session
from hinoki.schema import Schema, encode
from hinoki.sessions import Session, SessionSettings, SessionStore
from hinoki.settings import AppSettings, SecuritySettings
from hinoki.websocket import Transport, WebSocketRoute

if TYPE_CHECKING:
    from hinoki.app import Hinoki
    from hinoki.ratelimit import RateLimitPolicy

log = logging.getLogger("hinoki.runtime")

# A route the transport matched: the action number and decoded path parameters.
# `None` is the private marker for a path decoding error, deferred until after
# the pipeline's early host/CORS/redirect/docs policies.
MatchedRoute = tuple[int, dict[str, str] | None]
MAX_HEADER_SHAPES = 256
"""Distinct response header shapes kept; more are computed per request."""


class NativeBody(NamedTuple):
    """What a native handler reads beyond Rust's prepared inputs: the body
    source, the raw request headers and the raw query string (credentials and
    the body of a body method) and the TCP peer, from which the rate limit
    resolves its client like the full pipeline does."""

    source: BodySource | None
    headers: list[tuple[bytes, bytes]]
    query: bytes
    client: tuple[str, int] | None
    # The caller Rust already authenticated: the `sub` and every string scope.
    principal: tuple[str, list[str]] | None = None
    # Lower-cased header dict built directly from Rust's native header vector
    # when available, avoiding the intermediate list[tuple[bytes, bytes]].
    header_dict: dict[str, str] | None = None
    # Request handle used for lazy, single-header auth lookups without
    # materializing `header_dict`.
    header_request: core.PreparedRequest | None = None


def _native_headers(native: NativeBody) -> dict[str, str]:
    if native.header_dict is not None:
        return native.header_dict
    return core.header_dict(native.headers)


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


def native_input_exception(
    error: tuple[str, str, str | None, list[tuple[str, str, str]]],
) -> Exception:
    """Map Rust's bounded native input errors to the public pipeline errors."""
    code, message, available, details = error
    if code == "bad_path_encoding":
        mapped: Exception = HTTPError(HTTPStatus.BAD_REQUEST, code)
    elif code == "not_acceptable":
        mapped = HTTPError(
            HTTPStatus.NOT_ACCEPTABLE,
            code,
            available=available or "application/json",
        )
    elif code == "invalid_query":
        mapped = InvalidBody(code, message)
    elif code == "validation_error":
        mapped = ValidationError(
            [
                ErrorDetail(path, item_code, item_message)
                for path, item_code, item_message in details
            ]
        )
    else:
        mapped = RuntimeError(message)
    return mapped


class Pipeline:
    """One pipeline per worker process; it owns that worker's runtime state."""

    def __init__(self, app: Hinoki) -> None:
        check_resolved()
        self.app = app
        self.bindings = app.bindings
        self.rate_limit_metrics: core.RateLimitMetrics | None = None
        self.websockets = app.websockets
        self.crud_plan_selections: list[Any] = []
        self.crud_plan_reasons: list[str] = []
        self.startup_url = app.startup_url
        self.document = self._document(app)
        self.docs = docs_pages(app.openapi_url, app.docs_url, app.redoc_url)
        self._add_llms(app)
        self.startup_response = (
            Response.json(HTTPStatus.OK, {"status": "ok"})
            if self.startup_url is not None
            else None
        )
        self._check_startup_url()
        for path in self.docs:
            if app.lookup("GET", path) is not None:
                raise RegistrationError(
                    f"{path} is served by the documentation viewer and also "
                    "matches a route; move one of them"
                )
        self.ctx = ModuleContext(app.settings)
        self.events = app.events
        self.logging = (
            app.settings.logging if isinstance(app.settings, AppSettings) else None
        )
        self.debug = isinstance(app.settings, AppSettings) and app.settings.debug
        self.hosts = HostPolicy.of(app.settings)
        self.cors = CorsPolicy.of(app.settings)
        self.route_cors = [
            None if b.route.cors is None else CorsPolicy.from_cors(b.route.cors)
            for b in self.bindings
        ]
        self.has_route_cors = any(policy is not None for policy in self.route_cors)
        self.reminders = Reminders([b.route for b in self.bindings])
        settings = app.settings if isinstance(app.settings, AppSettings) else None
        self.security = settings.security if settings else SecuritySettings()
        self.trusted_proxies = tuple(
            ipaddress.ip_network(value, strict=False)
            for value in (settings.server.trusted_proxies if settings else ())
        )
        self.proxy_policy = build_proxy_policy(self.trusted_proxies)
        self.request_id_header = (
            settings.server.request_id_header.lower() if settings else "x-request-id"
        )
        self.trust_request_id = settings.server.trust_request_id if settings else False
        # Fixed for the worker's life: computed once, not for every request.
        self.request_id_name = self.request_id_header.encode("ascii")
        self.header_shapes: dict[
            object, tuple[tuple[tuple[bytes, bytes], ...], bool]
        ] = {}
        self.request_ids = itertools.count()
        self.request_id_prefix = secrets.token_hex(8)
        self.policy = {
            secure: headers_for(self.security, "https" if secure else "")
            for secure in (True, False)
        }
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        self._base_context: tuple[int, contextvars.Context] | None = None
        self._cached_services: tuple[int, Database | None, Mailer | None] | None = None
        self.streams = Streams(app.runtime.max_streams)
        needs_database = any(b.route.atomic for b in self.bindings)
        required_resources = tuple(
            (param.resource_type, binding.route.name)
            for binding in self.bindings
            for param in binding.resource_params
            if param.resource_type is not None
        )
        self.session_websockets = frozenset(
            id(route) for route in self.websockets if websocket_needs_session(route)
        )
        session_routes = (
            *(binding.route.name for binding in self.bindings if binding.needs_session),
            *(
                route.name
                for route in self.websockets
                if id(route) in self.session_websockets
            ),
        )
        session_resources = tuple(
            (kind, name)
            for name in session_routes
            for kind in (SessionStore, SessionSettings)
        )
        self.lifespan = Lifespan(
            app.modules,
            self.ctx,
            self.executor,
            app.runtime,
            needs_database=needs_database,
            required_resources=(*required_resources, *session_resources),
            on_startup=app.on_startup,
            on_shutdown=(*app.on_shutdown, self._release_rate_limits),
            events=app.events,
        )

    def _add_llms(self, app: Hinoki) -> None:
        """Render the llms.txt page once, at startup, from the OpenAPI document the
        worker already holds (or builds for it when the JSON is not served)."""
        if not app.llms_url:
            return
        if app.llms_url in self.docs:
            raise RegistrationError(
                f"{app.llms_url} is already used by the documentation viewer"
            )
        document = self.document if self.document is not None else app.openapi()
        text = render_llms(document, openapi_url=app.openapi_url)
        self.docs.update(llms_pages(app.llms_url, text))

    @staticmethod
    def _document(app: Hinoki) -> dict[str, Any] | None:
        """Build the OpenAPI document once, at startup, if it is served."""
        if app.openapi_url is None:
            return None
        if any(route.path == app.openapi_url for route in app.routes):
            raise RegistrationError(f"{app.openapi_url} is already used by a route")
        return app.openapi()

    def _check_startup_url(self) -> None:
        """Keep the fixed probe out of application and documentation routes."""
        path = self.startup_url
        if path is None:
            return
        if path == self.app.openapi_url or path in self.docs:
            raise RegistrationError(
                f"{path} is already used by the documentation viewer"
            )
        if any(
            self.app.lookup(route.method, path) is not None for route in self.app.routes
        ):
            raise RegistrationError(f"{path} is already matched by a route")

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
        scope, rid, response = await self._answer(scope, source, rid, route)
        return self.with_headers(response, scope, rid)

    async def handle_unfinished(
        self,
        scope: Scope,
        source: BodySource,
        rid: str,
        route: MatchedRoute | None = None,
        *,
        vetted: bool = False,
    ) -> Response:
        """``handle`` without the final headers, for a transport that adds them
        itself with the route headers this leaves in ``context.route_headers``
        (Kiso: ``PreparedRequest.respond_routed``). ``vetted`` is for a routed
        request whose transport already applied the readiness, size, host,
        preflight, HTTPS-redirect, probe and documentation policies of this
        pipeline (Kiso's ``ServerCore.classify``), so they are not repeated."""
        return (await self._answer(scope, source, rid, route, vetted=vetted))[2]

    async def _answer(
        self,
        scope: Scope,
        source: BodySource,
        rid: str | None,
        route: MatchedRoute | None,
        *,
        vetted: bool = False,
    ) -> tuple[Scope, str, Response]:
        scope, trusted = effective_scope(scope, self.proxy_policy)
        rid = rid or self._request_id(scope, trusted)
        response = await self._in_request_context(
            rid, self._respond(scope, source, rid, route, vetted=vetted)
        )
        return scope, rid, response

    async def handle_native(
        self,
        binding: Binding,
        rid: str,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        body: NativeBody | None = None,
        defer: bool = False,
    ) -> Response:
        """Run an eligible handler with Rust-prepared path and query inputs;
        ``body`` supplies the request body and headers of a body method."""
        return await self._in_request_context(
            rid, self._respond_native(binding, rid, args, input_error, body, defer)
        )

    async def handle_native_error(self, exc: Exception, rid: str) -> Response:
        """Translate an error raised after a native handler returned (such as
        deferred JSON encoding) inside the request context."""
        return await self._in_request_context(rid, self.native_failure(exc, rid))

    async def handle_native_failure(
        self, action: int, rid: str, failure: core.NativeFailure
    ) -> Response:
        """Translate a Rust CRUD failure through the application's error path."""
        return await self._in_request_context(
            rid, self._respond_native_failure(action, rid, failure)
        )

    async def admit_websocket(
        self,
        rid: str,
        route: WebSocketRoute,
        scope: Scope,
        params: dict[str, str] | None,
    ) -> Admitted | Response:
        """Judge a WebSocket handshake as an HTTP action is judged: the caller,
        the subprotocol and the arguments. A refusal is the HTTP answer to send
        instead of the upgrade, with the same error mapping as any action.
        ``params`` is ``None`` for a path that is not valid percent-encoding."""
        headers, _ = core.request_headers(scope.get("headers", []))
        return await self._in_request_context(
            rid, self._admit_websocket(rid, route, scope, headers, params)
        )

    async def _admit_websocket(
        self,
        rid: str,
        route: WebSocketRoute,
        scope: Scope,
        headers: dict[str, str],
        params: dict[str, str] | None,
    ) -> Admitted | Response:
        try:
            if params is None:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding")
            return await run_inline(
                self._admit_with_session(route, scope, headers, params),
                self.app.runtime.request_timeout,
            )
        except RequestDeadline:
            return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
        except ExecutorSaturated:
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        except Exception as exc:
            return await error_response(
                self._default_error(exc), rid, self.app.errors, self.debug
            )

    async def _admit_with_session(
        self,
        route: WebSocketRoute,
        scope: Scope,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> Admitted:
        """Give a session-authenticated handshake its session, read-only. The
        handshake's ``Origin`` was already checked against the route's and the
        CORS policy's origins, which is what keeps another site's page from
        opening it with the visitor's cookie."""
        session = (
            await self._request_session(headers)
            if id(route) in self.session_websockets
            else None
        )
        return await admit_handshake(
            route,
            headers,
            scope.get("query_string", b""),
            params,
            session=session,
        )

    async def run_websocket(
        self,
        rid: str,
        route: WebSocketRoute,
        transport: Transport,
        admitted: Admitted,
        path: str,
    ) -> None:
        """Run the handler of an accepted connection in the request context."""
        await self._in_request_context(
            rid, run_session(route, transport, admitted, path)
        )

    async def handle_native_rows(
        self, action: int, rid: str, fallback: core.NativeRows
    ) -> Response:
        """Hydrate a Rust-fetched row in Python without issuing another query."""
        return await self._in_request_context(
            rid, self._respond_native_rows(action, rid, fallback)
        )

    async def _respond_native_failure(
        self, action: int, rid: str, failure: core.NativeFailure
    ) -> Response:
        if failure[0] == "timeout":
            return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                try:
                    exception = self._default_error(
                        self._native_failure_exception(action, failure)
                    )
                except Exception as exc:
                    exception = exc
                return await self._native_error_response(exception, rid)
        except TimeoutError as exc:
            if limit.expired():
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            return await self._native_error_response(exc, rid)

    async def _native_error_response(self, error: Exception, rid: str) -> Response:
        return await error_response(error, rid, self.app.errors, self.debug)

    async def _respond_native_rows(
        self, action: int, rid: str, fallback: core.NativeRows
    ) -> Response:
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                try:
                    result = await self._hydrate_native_rows(action, fallback)
                    binding = self.bindings[action]
                    result = shape_result(binding, result)
                    body = core.encode_json(result, None, False, None)
                    return Response.rendered(
                        binding.route.status, body, "application/json", False
                    )
                except Exception as exc:
                    return await error_response(exc, rid, self.app.errors, self.debug)
        except TimeoutError as exc:
            if limit.expired():
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            return await error_response(exc, rid, self.app.errors, self.debug)

    async def _hydrate_native_rows(
        self, action: int, fallback: core.NativeRows
    ) -> object:
        selection = self.crud_plan_selections[action]
        if selection is None:
            raise RuntimeError("native rows fallback has no CRUD plan")
        if selection.action not in {"list", "retrieve"}:
            raise RuntimeError("native rows fallback supports read actions only")
        rows, page_info = fallback
        models = await selection.model.all()._hydrate(rows)
        items = [
            selection.resource.read(
                **{
                    name: getattr(model, name)
                    for name in selection.resource.read.specs()
                }
            )
            for model in models
        ]
        if selection.action == "list":
            if page_info is None:
                raise RuntimeError("native list fallback is missing page metadata")
            return ResultPage(items, *page_info)
        if not items:
            raise cast(
                Exception,
                selection.model.NotFound(f"{selection.model.__name__} not found"),
            )
        if len(items) != 1:
            raise RuntimeError("native item fallback must contain exactly one row")
        return items[0]

    def _native_failure_exception(
        self, action: int, failure: core.NativeFailure
    ) -> Exception:
        kind, message, constraint, table, sqlstate = failure
        if kind == "not_found":
            selection = self.crud_plan_selections[action]
            if selection is None:
                raise RuntimeError("native failure has no CRUD plan")
            return cast(Exception, selection.model.NotFound(message))
        exception_type: type[Exception] = {
            "unique": core.UniqueViolation,
            "foreign_key": core.ForeignKeyViolation,
            "not_null": core.NotNullViolation,
            "check": core.CheckViolation,
            "database": core.DatabaseError,
        }.get(kind, RuntimeError)
        exception = exception_type(message)
        if isinstance(exception, core.DatabaseError):
            exception.constraint = constraint
            exception.table = table
            exception.sqlstate = sqlstate
        return exception

    def _services(self) -> tuple[Database | None, Mailer | None]:
        version = self.ctx._version
        cached = self._cached_services
        if cached is not None and cached[0] == version:
            return cached[1], cached[2]
        db = self.ctx.find(Database)
        mailer = self.ctx.find(Mailer)
        self._cached_services = (version, db, mailer)
        return db, mailer

    async def _in_request_context[T](self, rid: str, response: Awaitable[T]) -> T:
        db, mailer = self._services()
        token = request_id.set(rid)
        context.route_headers.set(())
        context.rate_limit_headers.set(())
        context.route_cors.set(None)
        runner_token = context.sync_runner.set(self.executor.run)
        db_token = database_var.set(db)
        events_token = events_var.set(self.events)
        mailer_token = mailer_var.set(mailer)
        try:
            return await response
        finally:
            mailer_var.reset(mailer_token)
            events_var.reset(events_token)
            database_var.reset(db_token)
            context.sync_runner.reset(runner_token)
            request_id.reset(token)

    async def _respond_native(
        self,
        binding: Binding,
        rid: str,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        body: NativeBody | None = None,
        defer: bool = False,
    ) -> Response:
        response: Response
        try:
            async with asyncio.timeout(self.app.runtime.request_timeout) as limit:
                args = await self._native_arguments(binding, args, input_error, body)
                if defer and not binding.route.atomic:
                    response = await self._call_native(binding, args, body, defer=True)
                else:
                    response = await self._call_native(binding, args, body)
        except TimeoutError as exc:
            if limit.expired():
                response = HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            else:
                response = await error_response(exc, rid, self.app.errors, self.debug)
        except Exception as exc:
            response = await self.native_failure(exc, rid)
        return response

    def native_failure_now(self, exc: Exception, rid: str) -> Response | None:
        """``native_failure`` without a coroutine; ``None`` when an application
        error handler may need to await. Run it in the request's context."""
        if isinstance(exc, ExecutorSaturated):
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        return error_response_now(exc, rid, self.app.errors, self.debug)

    def crud_failure_now(
        self, action: int, rid: str, failure: core.NativeFailure
    ) -> Response | None:
        """The answer for a Rust CRUD failure that needs no coroutine, or
        ``None`` to translate it with ``handle_native_failure``."""
        if failure[0] == "timeout":
            return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
        ctx = self.native_context(rid)
        return ctx.run(self._crud_failure_now, action, rid, failure)

    def _crud_failure_now(
        self, action: int, rid: str, failure: core.NativeFailure
    ) -> Response | None:
        try:
            exception = self._default_error(
                self._native_failure_exception(action, failure)
            )
        except Exception as exc:
            exception = exc
        return error_response_now(exception, rid, self.app.errors, self.debug)

    async def native_failure(self, exc: Exception, rid: str) -> Response:
        """The answer for an error a native handler raised."""
        if isinstance(exc, ExecutorSaturated):
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        return await error_response(exc, rid, self.app.errors, self.debug)

    def _ensure_base_context(self) -> contextvars.Context:
        version = self.ctx._version
        cached = self._base_context
        if cached is not None and cached[0] == version:
            return cached[1]
        base = contextvars.Context()
        base.run(self._set_static_context)
        self._base_context = (version, base)
        return base

    def native_context(self, rid: str) -> contextvars.Context:
        """A fresh context holding what ``_in_request_context`` sets, for a
        handler Rust drives outside any task."""
        ctx = self._ensure_base_context().copy()
        ctx.run(request_id.set, rid)
        return ctx

    def _set_static_context(self) -> None:
        db, mailer = self._services()
        context.route_headers.set(())
        context.rate_limit_headers.set(())
        context.route_cors.set(None)
        context.sync_runner.set(self.executor.run)
        database_var.set(db)
        events_var.set(self.events)
        mailer_var.set(mailer)

    async def drive_native(
        self,
        binding: Binding,
        args: dict[str, object],
        native: NativeBody | None = None,
    ) -> Response:
        """The coroutine Rust drives for a native route: first, for a guarded
        route or a body method, the admission of the caller and the request
        values (``native`` carries the headers, the peer and the body), then
        the handler call and its encoding, inside the transaction of an
        ``atomic`` route, with no timeout and no error translation (the driver
        owns the deadline and the caller the errors). A guard that awaits
        something only asyncio can run hands the coroutine over like a handler
        does."""
        if native is not None:
            args = await self._native_arguments(binding, args, None, native)
        return await self._call_native(
            binding, args, native, defer=not binding.route.atomic
        )

    async def native_continue(
        self,
        rid: str,
        work: Awaitable[Response],
        timeout: float | None,
        reason: str | None = None,
    ) -> Response:
        """Finish a driven handler that awaited something only asyncio can
        run, under what is left of the request timeout. A handler already
        cancelled (``reason``) only unwinds: its cleanup is not cancelled again,
        and a deadline that ends it is the ``504``."""
        if reason is not None:
            return await self._unwind(rid, work, reason)
        response: Response
        try:
            async with asyncio.timeout(timeout) as limit:
                response = await work
        except TimeoutError as exc:
            if limit.expired():
                response = HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            else:
                response = await error_response(exc, rid, self.app.errors, self.debug)
        except Exception as exc:
            response = await self.native_failure(exc, rid)
        return response

    async def _unwind(
        self, rid: str, work: Awaitable[Response], reason: str
    ) -> Response:
        try:
            return await work
        # Only our own deadline is absorbed, any other cancellation is re-raised.
        except asyncio.CancelledError:  # NOSONAR
            task = asyncio.current_task()
            if reason == "deadline" and task is not None and task.cancelling() == 0:
                return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
            raise
        except Exception as exc:
            return await self.native_failure(exc, rid)

    def _request_id(self, scope: Scope, trusted: bool) -> str:
        """The trusted inbound request id when configured and valid, else a new
        one."""
        incoming = None
        if trusted and self.trust_request_id:
            incoming, saw_header = inbound_request_id_checked(
                scope.get("headers", ()), self.request_id_name
            )
            if incoming is None and saw_header:
                log.warning("ignored invalid request ID from trusted proxy")
        return incoming or self._new_request_id()

    def new_request_id(self) -> str:
        """A fresh request id, for a connection that has no HTTP request to
        take one from."""
        return self._new_request_id()

    def _new_request_id(self) -> str:
        """32 hex characters, unique within the process (random per-worker
        prefix and a counter), without a system call per request."""
        return f"{self.request_id_prefix}{next(self.request_ids):016x}"

    def shaped_headers(
        self, response: Response, https: bool
    ) -> tuple[tuple[tuple[bytes, bytes], ...], bool]:
        """The final headers but the request id, and whether the answer set its
        own ``allow_origin``. Fixed by the answer's own headers, the policy and
        the route's headers, so each distinct shape is computed once; a shape
        beyond the bound (headers that carry per-request values) is not kept."""
        route_headers = context.route_headers.get()
        try:
            key: object = (https, route_headers, tuple(response.headers))
            shape = self.header_shapes.get(key)
        except TypeError:  # an unhashable header entry
            key, shape = None, None
        if shape is None:
            headers, has_allow_origin = core.response_headers(
                response.headers,
                self.policy[https],
                route_headers,
                self.request_id_name,
                b"",
                ALLOW_ORIGIN,
            )
            shape = (tuple(headers[:-1]), has_allow_origin)
            if key is not None and len(self.header_shapes) < MAX_HEADER_SHAPES:
                self.header_shapes[key] = shape
        return shape

    def with_headers(self, response: Response, scope: Scope, rid: str) -> Response:
        """Replace the answer's headers with the final ones."""
        shaped, has_allow_origin = self.shaped_headers(
            response, scope.get("scheme", "") == "https"
        )
        headers = [*shaped, (self.request_id_name, rid.encode("ascii"))]
        extra = context.rate_limit_headers.get()
        if extra:
            headers.extend((name.encode(), value.encode()) for name, value in extra)
        policy = context.route_cors.get() or self.cors
        if policy is not None and not has_allow_origin:
            # An answer that sets its own CORS headers keeps them.
            headers.extend(policy.headers_from_scope(scope.get("headers", ())))
        stream = response.stream
        if stream is not None:
            length = declared_length(stream)
            if length is not None:
                headers.append((b"content-length", str(length).encode()))
        elif response.status not in BODYLESS:
            headers.append((b"content-length", str(len(response.body)).encode()))
        # A new object: the answer may belong to a reused exception instance.
        return Response(response.status, response.body, headers, stream)

    async def _respond(
        self,
        scope: Scope,
        source: BodySource,
        rid: str,
        route: MatchedRoute | None,
        *,
        vetted: bool = False,
    ) -> Response:
        try:
            return await run_inline(
                self._serve(scope, source, route, vetted=vetted),
                self.app.runtime.request_timeout,
            )
        except RequestDeadline:
            return HTTPError(HTTPStatus.GATEWAY_TIMEOUT, "timeout").response
        except ExecutorSaturated:  # a plain-def hook found the pool full
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        except Exception as exc:
            return await error_response(
                self._default_error(exc), rid, self.app.errors, self.debug
            )

    def _default_error(self, error: Exception) -> Exception:
        if (
            isinstance(error, core.UniqueViolation)
            and self.app.errors.find(error) is None
        ):
            return Conflict()
        return error

    async def _serve(
        self,
        scope: Scope,
        source: BodySource,
        route: MatchedRoute | None,
        *,
        vetted: bool = False,
    ) -> Response:
        """Ready check, host and preflight policies, routing, authentication,
        then (only if the action takes a body) reading and validating it, then
        the handler. A ``vetted`` routed request skips the policies its
        transport already applied."""
        if not self.lifespan.ready:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", RETRY_LATER)
        headers, header_total = core.request_headers(scope.get("headers", []))
        if not (vetted and route is not None):
            self._check_request_sizes(scope, header_total)
            early = await self._early_response(scope, headers)
            if early is not None:
                return early
        binding, path_params = self._resolve_request(scope, route)
        if binding.route.middleware:
            return await self._through_middleware(
                scope, source, binding, path_params, headers
            )
        return await self._dispatch_request(
            scope, source, binding, path_params, headers
        )

    async def _through_middleware(
        self,
        scope: Scope,
        source: BodySource,
        binding: Binding,
        path_params: dict[str, str],
        headers: dict[str, str],
    ) -> Response:
        """Run the action's middleware around the rest of the request. The
        innermost step translates an error into its answer, so a middleware
        always receives a ``Response`` and the action's transaction is already
        rolled back; one that raises is translated by the caller."""
        rid = request_id.get() or ""
        head = MiddlewareRequest(
            method=scope["method"],
            path=scope["path"],
            route=binding.route.path,
            headers=MappingProxyType(headers),
            query=scope.get("query_string", b"").decode("latin-1"),
            client=scope.get("client"),
            scheme=scope.get("scheme", ""),
            request_id=rid,
        )
        chain = binding.route.middleware
        produced: list[StreamHandle] = []

        async def action() -> HandlerResponse:
            try:
                response = await self._dispatch_request(
                    scope, source, binding, path_params, headers
                )
            except Exception as exc:
                response = await self._translated(exc, rid)
            if response.stream is not None:
                produced.append(response.stream)
                return response.stream.answer
            return HandlerResponse.raw(response.status, response.body, response.headers)

        async def call(index: int) -> HandlerResponse:
            if index == len(chain):
                return await action()
            called = False

            async def call_next() -> HandlerResponse:
                nonlocal called
                if called:
                    raise RuntimeError("call_next() was already called")
                called = True
                return await call(index + 1)

            answer = await chain[index](head, call_next)
            if not isinstance(answer, HandlerResponse):
                raise TypeError(
                    f"middleware {type(chain[index]).__name__} must return a "
                    f"Response, got {type(answer).__name__}"
                )
            return answer

        try:
            final = await call(0)
        except BaseException:
            await self._drop(produced)
            raise
        return await self._settle(final, produced)

    async def _drop(self, produced: list[StreamHandle]) -> None:
        """Close the streams a middleware chain left unsent."""
        for handle in produced:
            await discard(handle, self.executor)
        produced.clear()

    async def _settle(
        self, final: HandlerResponse, produced: list[StreamHandle]
    ) -> Response:
        """The answer after the middleware chain: the handler's stream when the
        chain kept its body (a header added, for instance), a new stream the
        chain built, or a complete response (the handler's stream is then
        closed without being sent)."""
        if not isinstance(final, Streamed):
            await self._drop(produced)
            return Response(final.status, final.body, list(final.headers))
        if produced and produced[0].answer.same_body(final):
            handle = produced[0]
            handle.answer = final
            return Response(final.status, b"", list(final.headers), handle)
        await self._drop(produced)
        return await self._new_stream(final)

    async def _new_stream(self, answer: Streamed) -> Response:
        """A stream that a middleware created, with its own place."""
        lease = self.streams.lease()
        if lease is None:
            await discard_answer(answer, self.executor)
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER)
        try:
            response = await self._stream_response(answer)
        except BaseException:
            lease.release()
            raise
        if response.stream is not None:
            response.stream.lease = lease
        return response

    async def _translated(self, exc: Exception, rid: str) -> Response:
        """The answer for an error raised while serving a request."""
        if isinstance(exc, ExecutorSaturated):
            return HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ).response
        return await error_response(
            self._default_error(exc), rid, self.app.errors, self.debug
        )

    async def _early_response(  # noqa: PLR0911 - direct response per early policy
        self, scope: Scope, headers: dict[str, str]
    ) -> Response | None:
        """Apply request policies and serve generated documentation early."""
        if (
            self.hosts is not None
            or self.cors is not None
            or self.has_route_cors
            or self.security.redirect_https
        ):
            preflight = self._guard(scope, headers)
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
        startup = self._startup_response(scope, headers)
        if startup is not None:
            return startup
        if self.document is not None and self._is_document_request(scope):
            return Response.json(HTTPStatus.OK, self.document)
        page = (
            self.docs.get(scope["path"]) if scope["method"] in {"GET", "HEAD"} else None
        )
        if page is not None:
            return docs_response(page)
        return None

    def _startup_response(
        self, scope: Scope, headers: dict[str, str]
    ) -> Response | None:
        """Respond to the configured startup probe without resolving a route."""
        path = self.startup_url
        if path is None:
            return None
        raw_path = scope.get("raw_path")
        if raw_path is None:
            if scope.get("path") != path:
                return None
        elif raw_path != path.encode("ascii"):
            return None
        method = scope["method"]
        if method not in {"GET", "HEAD"}:
            raise HTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                [(b"allow", b"GET, HEAD")],
            )
        if negotiate(headers.get("accept", ""), (JSONRenderer(),)) is None:
            raise HTTPError(
                HTTPStatus.NOT_ACCEPTABLE,
                "not_acceptable",
                available="application/json",
            )
        parse_query(scope.get("query_string", b""))
        if self.startup_response is None:
            raise RuntimeError("startup response was not initialized")
        return self.startup_response

    def _resolve_request(
        self, scope: Scope, route: MatchedRoute | None
    ) -> tuple[Binding, dict[str, str]]:
        """Match a route (unless the transport did) and establish its
        response-header context."""
        if route is None:
            action, path_params = self._route(scope)
        else:
            if route[1] is None:
                raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding")
            action, path_params = route[0], route[1]
        binding = self.bindings[action]
        context.route_headers.set(binding.owner_headers)
        context.route_cors.set(self.route_cors[action])
        self.reminders.called(binding.route)
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
        session: Session | None = None
        if binding.needs_session:
            session = await self._request_session(headers)
            self._check_session_csrf(binding, scope, headers, session)
        if access is not None:
            # Parsed once: authentication and the request share the result.
            query = parse_query(scope.get("query_string", b""))
            principal = await authorize(
                access,
                headers,
                query,
                route=binding.route.path,
                method=binding.route.method,
                session=session,
            )
        if binding.route.rate_limit is not None:
            await self._rate_limit(binding, scope.get("client"), principal)
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
        request: Request | None = None
        if binding.needs_request:
            request, data = build_request(
                scope, binding, path_params, headers, raw_body, principal, query
            )
        else:
            data = plain_data(scope, path_params, principal, query)
        return await self._invoke(
            binding, request, renderer, binding.collect(data), session
        )

    async def _request_session(self, headers: dict[str, str]) -> Session:
        """Load the route's single session from its cookie before auth."""
        settings = self.ctx.get(SessionSettings)
        cookies = core.parse_cookie_header(headers.get("cookie", ""))
        return await Session.from_cookie(
            self.ctx.get(SessionStore), cookies.get(settings.cookie_name), settings
        )

    def _check_session_csrf(
        self,
        binding: Binding,
        scope: Scope,
        headers: dict[str, str],
        session: Session,
    ) -> None:
        """Reject cross-site unsafe requests using a session cookie."""
        if binding.route.method in {"GET", "HEAD", "OPTIONS"} or session.is_new:
            return
        if not same_site_origin(
            headers.get("origin"),
            headers.get("sec-fetch-site", ""),
            headers.get("host", ""),
            str(scope.get("scheme") or "http"),
        ):
            raise HTTPError(HTTPStatus.FORBIDDEN, "csrf_rejected")
        csrf = headers.get("x-csrf-token", "")
        if not csrf or not session.verify_csrf(csrf):
            raise HTTPError(HTTPStatus.FORBIDDEN, "csrf_rejected")

    async def _rate_limit(
        self,
        binding: Binding,
        client: tuple[str, int] | None,
        principal: object | None,
    ) -> None:
        """Ask the route's policy before the body is read. Only the policy's own
        ``TooManyRequests`` refuses with 429; any other failure of the policy or
        its store fails closed with a generic 503 that names nothing."""
        route = binding.route
        policy = cast("RateLimitPolicy", route.rate_limit)
        context = RateLimitContext(
            method=route.method,
            route=route.path,
            client=str(client[0]) if client and client[0] else None,
            caller=cast("Caller[Any] | None", principal),
            lookup=self.ctx.get,
        )
        if type(policy).check is RateLimit.check:
            await self._builtin_limit(cast("RateLimit", policy), context, route)
            return
        try:
            await self._call_policy(policy, context)
        except TooManyRequests as refusal:
            if not valid_refusal(refusal):
                log.error(
                    "the rate limit policy of %s %s failed", route.method, route.path
                )
                self._record_limit("unavailable")
                raise HTTPError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "rate_limit_unavailable",
                    RETRY_LATER,
                ) from None
            self._record_limit("refused")
            await announce_rate_limited(policy, route, refusal)
            raise
        except ExecutorSaturated:
            self._record_limit("unavailable")
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ) from None
        except Exception:
            log.error("the rate limit policy of %s %s failed", route.method, route.path)
            self._record_limit("unavailable")
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "rate_limit_unavailable", RETRY_LATER
            ) from None
        self._record_limit("allowed")

    async def _call_policy(
        self, policy: RateLimitPolicy, context: RateLimitContext
    ) -> None:
        if inspect.iscoroutinefunction(policy.check):
            await policy.check(context)
            return
        result = await self.executor.run(policy.check, context)
        if inspect.isawaitable(result):
            await result

    def _record_limit(self, decision: str) -> None:
        if self.rate_limit_metrics is not None:
            self.rate_limit_metrics.record(decision)

    def _builtin_limiters(self) -> Iterator[RateLimit]:
        for binding in self.bindings:
            policy = binding.route.rate_limit
            if isinstance(policy, RateLimit) and type(policy).check is RateLimit.check:
                yield policy

    def attach_rate_limit_metrics(self, metrics: core.RateLimitMetrics | None) -> None:
        """Count rate limit decisions into the server's metrics: the built-in
        limiters count themselves, the pipeline counts the application's policies."""
        if metrics is None:
            return
        self.rate_limit_metrics = metrics
        for policy in self._builtin_limiters():
            policy.attach_metrics(metrics)

    async def _release_rate_limits(self, _: ModuleContext) -> None:
        """At shutdown, drop what the limiters bound to this worker's database."""
        self.rate_limit_metrics = None
        for policy in self._builtin_limiters():
            policy.release()

    def uses_rate_limit(self) -> bool:
        return any(binding.route.rate_limit is not None for binding in self.bindings)

    async def _builtin_limit(
        self, policy: RateLimit, limit_context: RateLimitContext, route: Route
    ) -> None:
        """The built-in limiter: no executor, the decision is an awaitable of the
        native core. Any failure of the limiter or its storage fails closed with
        503 (``on_error="closed"``; ``"open"`` never raises)."""
        try:
            decision = await policy.decide(limit_context)
        except Exception as error:
            if not isinstance(error, core.RateLimitUnavailable):
                self._record_limit("unavailable")  # Rust counted the storage ones
            log.error("the rate limit of %s %s failed", route.method, route.path)
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "rate_limit_unavailable", RETRY_LATER
            ) from None
        if not decision.allowed:
            refusal = policy.refusal(decision)
            await announce_rate_limited(policy, route, refusal)
            raise refusal
        extra = policy.header_items(decision)
        if extra:
            context.rate_limit_headers.set(extra)

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

    def _guard(self, scope: Scope, headers: dict[str, str]) -> Response | None:
        """Refuse a host that is not allowed; answer a CORS preflight."""
        if self.hosts is not None:
            self.hosts.check(headers.get("host", ""))
        policy = self._preflight_policy(scope, headers)
        if policy is None:
            return None
        if policy is self.cors:
            return policy.answer(scope["method"], headers)
        try:
            response = policy.answer(scope["method"], headers)
        except HTTPError:
            # A refusal carries this route's policy.
            context.route_cors.set(policy)
            raise
        if response is not None:
            context.route_cors.set(policy)
        return response

    def _preflight_policy(
        self, scope: Scope, headers: dict[str, str]
    ) -> CorsPolicy | None:
        """The policy that answers a preflight: the one of the route the browser
        asks about (the method in ``Access-Control-Request-Method`` on this path)
        when it declares its own, else the application's."""
        asked = headers.get("access-control-request-method")
        if not self.has_route_cors or scope["method"] != "OPTIONS" or not asked:
            return self.cors
        raw_path = scope.get("raw_path")
        try:
            path = scope["path"] if raw_path is None else self.raw_target(raw_path)
        except HTTPError:
            return self.cors
        found = self.app.lookup(asked, path)
        if isinstance(found, Found):
            return self.route_cors[found.action] or self.cors
        return self.cors

    def _is_document_request(self, scope: Scope) -> bool:
        return bool(
            scope["method"] in {"GET", "HEAD"} and scope["path"] == self.app.openapi_url
        )

    def _route(self, scope: Scope) -> tuple[int, dict[str, str]]:
        raw_path = scope.get("raw_path")
        path = scope["path"] if raw_path is None else self.raw_target(raw_path)
        found = self.app.lookup(scope["method"], path)
        if found is None:
            raise HTTPError(HTTPStatus.NOT_FOUND, "not_found")
        if isinstance(found, MethodNotAllowed):
            allow = ", ".join(found.allowed).encode()
            raise HTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", [(b"allow", allow)]
            )
        params = found.params if raw_path is None else decode_params(found.params)
        return found.action, params

    @staticmethod
    def raw_target(raw_path: bytes) -> str:
        try:
            return raw_path.split(b"?", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "bad_path_encoding") from None

    async def _invoke(
        self,
        binding: Binding,
        request: Request | None,
        renderer: Renderer,
        kwargs: dict[str, object],
        session: Session | None = None,
    ) -> Response:
        """Run the handler; with ``atomic=True`` inside a transaction that
        commits only after the output is encoded, and rolls back on any error
        before it becomes an HTTP response."""
        if binding.session_params and session is None:
            if request is None:
                raise RuntimeError("a handler requiring Session has no request")
            session = await Session.from_cookie(
                self.ctx.get(SessionStore),
                request.COOKIES.get(self.ctx.get(SessionSettings).cookie_name),
                self.ctx.get(SessionSettings),
            )
            kwargs = {**kwargs, binding.session_params[0].name: session}
        if session is not None and binding.session_params:
            kwargs = {**kwargs, binding.session_params[0].name: session}
        response = await self._invoke_bound(binding, request, renderer, kwargs)
        if session is not None:
            cookie = await session.commit()
            if cookie is not None:
                response.headers.append(encode_header(*cookie))
        return response

    async def _invoke_bound(
        self,
        binding: Binding,
        request: Request | None,
        renderer: Renderer,
        kwargs: dict[str, object],
    ) -> Response:
        """Keep streaming and transaction handling behind the session commit."""
        if binding.streams:
            return await self._invoke_streaming(binding, request, renderer, kwargs)
        if binding.route.atomic:
            stream_to_discard: Streamed | None = None
            try:
                async with self.ctx.get(Database).atomic():
                    response = await self._call(binding, request, renderer, kwargs)
                    if response.stream is not None:
                        stream_to_discard = response.stream.answer
                        raise ResponseContractError(
                            f"{binding.route.name}: atomic=True cannot return a "
                            "streamed response"
                        )
            finally:
                if stream_to_discard is not None:
                    await discard_answer(stream_to_discard, self.executor)
            return response
        if binding.stream_capable:
            return await self._invoke_streaming(binding, request, renderer, kwargs)
        return await self._call(binding, request, renderer, kwargs)

    async def _invoke_streaming(
        self,
        binding: Binding,
        request: Request | None,
        renderer: Renderer,
        kwargs: dict[str, object],
    ) -> Response:
        """Run an action that streams: one of the worker's ``max_streams``
        places is taken before the handler runs (a full worker answers 503 and
        the handler does not run), and kept until the head is sent."""
        lease = self.streams.lease()
        if lease is None:
            raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER)
        try:
            response = await self._call(binding, request, renderer, kwargs)
        except BaseException:
            lease.release()
            raise
        if response.stream is None:
            lease.release()
        else:
            response.stream.lease = lease
        return response

    async def _streamed(self, binding: Binding, answer: Streamed) -> Response:
        """The answer for a ``Stream``, ``EventStream`` or ``File`` a handler
        returned; its body is closed unsent when the answer is refused."""
        try:
            if not binding.stream_capable:
                raise ResponseContractError(
                    f"{binding.route.name} returned a {type(answer).__name__} "
                    "but its return annotation does not name it; annotate the "
                    f"action with -> {type(answer).__name__}"
                )
            enforce_response(binding, answer)
            return await self._stream_response(answer)
        except BaseException:
            await discard_answer(answer, self.executor)
            raise

    async def _stream_response(self, answer: Streamed) -> Response:
        location = None
        if isinstance(answer, File):
            try:
                location = await core.locate_file(answer.path, answer.root)
            except core.FileLookupBusy as exc:
                raise ExecutorSaturated("every file lookup place is taken") from exc
            if location is None:
                raise HTTPError(HTTPStatus.NOT_FOUND, "not_found")
        return Response(
            answer.status, b"", list(answer.headers), StreamHandle(answer, location)
        )

    async def _call(
        self,
        binding: Binding,
        request: Request | None,
        renderer: Renderer,
        kwargs: dict[str, object],
    ) -> Response:
        route = binding.route
        row_wire = (
            binding.row_wire
            if binding.row_wire is not None
            and (binding.default_json or is_default_json_renderer(renderer))
            else None
        )
        result = await self._handler_result(binding, request, kwargs, row_wire)
        if isinstance(result, Streamed):
            return await self._streamed(binding, result)
        if isinstance(result, HandlerResponse):
            return Response(result.status, result.body, result.headers)
        exact = enforce_response(binding, result)
        if result is not None and route.status not in BODYLESS:
            result = shape_result(binding, result, exact)
            shown = result if renderer.consumes_objects else encode(result)
            if request is None:  # only the stock JSON renderer runs without one
                body = core.encode_json(shown, None, False, None)
            else:
                body = renderer.render(shown, request)
            many = len(route.renderers) > 1
            return Response.rendered(route.status, body, renderer.media_type, many)
        no_content = route.status == HTTPStatus.OK
        return Response(HTTPStatus.NO_CONTENT if no_content else route.status)

    async def _native_authorize(
        self,
        binding: Binding,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        native: NativeBody | None,
    ) -> dict[str, object]:
        """Authenticate before anything else the request carries is judged, in
        the order of the full pipeline: a bad path encoding and a malformed
        query string first, then the credentials, then the rate limit.
        Negotiation, body and validation errors wait until the caller is
        known and admitted."""
        access = binding.route.access
        if native is None:
            raise RuntimeError(  # pragma: no cover - routing excludes this state
                "native guarded route without its request data"
            )
        if input_error is not None and input_error[0] == "bad_path_encoding":
            raise native_input_exception(input_error)
        principal = None
        if access is not None and native.principal is not None:
            subject, scopes = native.principal
            principal = Caller(subject, None, frozenset(scopes))
        elif access is not None:
            query = parse_query(native.query) if native.query else {}
            headers = (
                _LazyHeaders(native.header_request)
                if native.header_dict is None and native.header_request is not None
                else _native_headers(native)
            )
            principal = await authorize(
                access,
                headers,
                query,
                route=binding.route.path,
                method=binding.route.method,
            )
        if binding.route.rate_limit is not None:
            client = effective_client(native.client, native.headers, self.proxy_policy)
            await self._rate_limit(binding, client, principal)
        name = binding.principal_name
        return args if name is None else {**args, name: principal}

    async def _native_arguments(
        self,
        binding: Binding,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        body: NativeBody | None,
    ) -> dict[str, object]:
        """Admit the caller, then judge the request values: the handler
        arguments, or the error the full pipeline would answer."""
        route = binding.route
        if route.access is not None or route.rate_limit is not None:
            args = await self._native_authorize(binding, args, input_error, body)
        if binding.body_param is not None:
            return await self._native_body_args(binding, args, input_error, body)
        if binding.query_param is not None:
            return self._native_query_args(binding, args, input_error, body)
        if input_error is not None:
            raise native_input_exception(input_error)
        return args

    def _native_query_args(
        self,
        binding: Binding,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        native: NativeBody | None,
    ) -> dict[str, object]:
        """The handler arguments of a ``Query[Schema]`` action: a malformed
        query string first, then every path and query problem in declaration
        order."""
        param = binding.query_param
        if param is None or native is None:
            raise RuntimeError(  # pragma: no cover - routing excludes this state
                "native query route without its request data"
            )
        invalid = input_error is not None and input_error[0] == "validation_error"
        if input_error is not None and not invalid:
            raise native_input_exception(input_error)
        query = parse_query(native.query)
        errors = (
            [ErrorDetail(*item) for item in input_error[3]]
            if invalid and input_error
            else []
        )
        value: object = None
        try:
            value = param.from_queryschema(RequestData(query=query))
        except ValidationError as exc:
            errors.extend(exc.details)
        if errors:
            order = {name: i for i, name in enumerate(binding.native_input_names)}
            errors.sort(key=lambda d: order.get(d.path.split(".", 1)[0], 0))
            raise ValidationError(errors)
        return binding.complete_native_args(args, (param.name, value))

    @staticmethod
    def _require_body_source(
        body: NativeBody | None,
    ) -> tuple[NativeBody, BodySource]:
        if body is None or body.source is None:
            raise RuntimeError(  # pragma: no cover - routing excludes this state
                "native body route without its request data"
            )
        return body, body.source

    async def _native_body_args(
        self,
        binding: Binding,
        args: dict[str, object],
        input_error: tuple[str, str, str | None, list[tuple[str, str, str]]] | None,
        body: NativeBody | None,
    ) -> dict[str, object]:
        """The handler arguments of a body method, failing in the order of the
        full pipeline: path and negotiation, body read, query syntax, body
        parse, then every path, query and body problem in declaration order."""
        param = binding.body_param
        if param is None:
            raise RuntimeError(  # pragma: no cover - routing excludes this state
                "native body route without a body parameter"
            )
        invalid = input_error is not None and input_error[0] == "validation_error"
        if (
            input_error is not None
            and not invalid
            and input_error[0] != "invalid_query"
        ):
            raise native_input_exception(input_error)
        body, source = self._require_body_source(body)
        headers = _native_headers(body)
        raw = await self._body(source, headers.get("content-length", ""))
        if input_error is not None and not invalid:
            raise native_input_exception(input_error)
        content_type = headers.get("content-type", "")
        parser = choose(binding.route.parsers, media_type(content_type))
        errors = (
            [ErrorDetail(*item) for item in input_error[3]]
            if invalid and input_error
            else []
        )
        typed = self._typed_body(param, parser, raw) if not errors else None
        if typed is not None:
            return binding.complete_native_args(args, (param.name, typed))
        parsed = parser.parse(raw, content_type)
        tree = parsed.fields if param.text and parser.forms else parsed.data
        value: object = None
        try:
            value = param.from_body(RequestData(body=tree))
        except ValidationError as exc:
            errors.extend(exc.details)
        if errors:
            order = {name: i for i, name in enumerate(binding.native_input_names)}
            errors.sort(key=lambda d: order.get(d.path.split(".", 1)[0], 0))
            raise ValidationError(errors)
        return binding.complete_native_args(args, (param.name, value))

    @staticmethod
    def _typed_body(param: Param, parser: Parser, raw: bytes) -> object | None:
        """The body parameter read from the bytes in one pass by Rust, or
        ``None`` when the ordinary parse and validation must run (a custom or
        non-JSON parser, a form source, invalid input, hooks or checks)."""
        schema = param.schema
        if schema is None or param.text or type(parser) is not JSONParser:
            return None
        if not is_native_parser(parser):
            return None
        return schema.validate_json(raw)

    async def _call_native(
        self,
        binding: Binding,
        args: dict[str, object],
        native: NativeBody | None = None,
        defer: bool = False,
    ) -> Response:
        """Run a selected no-Request JSON handler and encode through Rust; with
        ``atomic=True`` the commit follows the encoding, as in ``_invoke``."""
        if not binding.route.atomic:
            return await self._native_result(binding, args, native, defer=defer)
        async with self.ctx.get(Database).atomic():
            return await self._native_result(binding, args, native, defer=defer)

    async def _native_result(
        self,
        binding: Binding,
        args: dict[str, object],
        native: NativeBody | None = None,
        defer: bool = False,
    ) -> Response:
        route = binding.route
        result = await self._handler_result(
            binding, None, binding.complete_native_args(args)
        )
        if isinstance(result, HandlerResponse):
            return await self._plain_response(binding, result)
        # A deferred exact list is checked while Rust encodes it (`Response.strict`).
        fused = defer and binding.fused_check and type(result) is list
        exact = False if fused else enforce_response(binding, result)
        if result is not None and route.status not in BODYLESS:
            result = shape_result(binding, result, exact)
            return self._native_rendered(
                binding, result, native, defer=defer, strict=fused
            )
        if route.status == HTTPStatus.OK:
            return Response(HTTPStatus.NO_CONTENT)
        return Response(route.status)

    async def _plain_response(
        self, binding: Binding, result: HandlerResponse
    ) -> Response:
        if isinstance(result, Streamed):  # a native handler is never annotated
            return await self._streamed(binding, result)  # pragma: no cover
        return Response(result.status, result.body, result.headers)

    def _native_rendered(
        self,
        binding: Binding,
        result: object,
        native: NativeBody | None,
        defer: bool = False,
        strict: bool = False,
    ) -> Response:
        """Render with the renderer ``Accept`` selects among the route's
        built-in renderers, which never read the ``Request``."""
        route = binding.route
        if binding.default_json:
            if defer:
                return Response(
                    route.status,
                    b"",
                    [(b"content-type", b"application/json")],
                    payload=result,
                    strict=strict,
                )
            body = core.encode_json(result, None, False, None)
            return Response.rendered(route.status, body, "application/json", False)
        if len(route.renderers) == 1:
            renderer = route.renderers[0]
        else:
            if native is None:
                raise RuntimeError(  # pragma: no cover - routing excludes this state
                    "native multi-renderer route without its headers"
                )
            accept = _native_headers(native).get("accept", "")
            chosen = negotiate(accept, route.renderers)
            if chosen is None:
                raise RuntimeError(  # pragma: no cover - routing excludes this state
                    "native route answered an unacceptable request"
                )
            renderer = chosen
        shown = result if renderer.consumes_objects else encode(result)
        body = renderer.render(shown, cast("Request", None))
        many = len(route.renderers) > 1
        return Response.rendered(route.status, body, renderer.media_type, many)

    async def _handler_result(
        self,
        binding: Binding,
        request: Request | None,
        kwargs: dict[str, object],
        row_wire: object | None = None,
    ) -> object:
        """Call an async handler in its task or a sync handler in the executor."""
        route = binding.route
        if binding.resource_params:
            kwargs = {
                **kwargs,
                **{
                    param.name: self.ctx.get(param.resource_type)
                    for param in binding.resource_params
                    if param.resource_type is not None
                },
            }
        if row_wire is not None:
            kwargs = {**kwargs, "_row_plan": row_wire}
        owner = (route.owner(),) if route.owner is not None else ()
        if binding.takes_request:
            if request is None:
                raise RuntimeError("a handler requiring Request has no request")
            args = (*owner, request)
        else:
            args = owner
        try:
            if binding.is_async:
                handler = cast("Callable[..., Awaitable[object]]", route.handler)
                return await handler(*args, **kwargs)
            result = await self.executor.run(route.handler, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except ExecutorSaturated:
            raise HTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE, "overloaded", RETRY_LATER
            ) from None


def enforce_response(binding: Binding, result: object) -> bool:
    """With ``validate_response=True``, refuse a result the annotation does not
    allow; the error names the action, never the value. ``True`` when the same
    pass proved a list holds exactly the declared item Schema (nothing to narrow)."""
    check = binding.response_check
    if check is None or binding.route.status in BODYLESS:
        return False
    scan = binding.response_scan
    if scan is not None:
        verdict = scan(result)
        if verdict:
            return verdict == 1
    elif check(result):
        return False
    if binding.response_schema is not None and isinstance(result, Model):
        return False  # a Model is projected through the declared Schema right after
    raise ResponseContractError(
        f"{binding.route.name} returned a {type(result).__name__} that does not "
        f"match its return annotation {binding.response_annotation}"
    )


async def announce_rate_limited(
    policy: object, route: Route, refusal: TooManyRequests
) -> None:
    """Tell the application's ``RateLimited`` handlers, when it has any; the
    429 does not depend on them."""
    bus = listener(RateLimited)
    if bus is not None:
        await bus.notify(
            RateLimited(
                type(policy).__qualname__, route.path, route.method, refusal.retry_after
            )
        )


def valid_refusal(error: TooManyRequests) -> bool:
    """Whether a refusal carries a whole ``retry_after`` of 1..86,400 seconds and
    the matching header (an error built without its constructor does not)."""
    seconds = getattr(error, "retry_after", None)
    headers = getattr(error, "headers", None)
    return (
        type(seconds) is int
        and 1 <= seconds <= MAX_RETRY_AFTER
        and isinstance(headers, dict)
        and headers.get("retry-after") == str(seconds)
    )


class ResponseProjectionError(RuntimeError):
    """The handler's own return value does not satisfy its response Schema.

    It is a server fault, answered as ``500 internal_error`` and logged with the
    original ``ValidationError`` as its cause; no constraint detail reaches the
    client."""


def shape_result(binding: Binding, result: object, exact: bool = False) -> object:
    """A handler result as the declared annotation shows it: narrowed to the
    Schema its annotation names, wherever the annotation holds one."""
    if binding.narrow_schema is not None:
        return narrow_result(binding.narrow_schema, result, exact)
    if binding.narrow_guide is not None:
        return binding.narrow_guide.narrow(result)
    return project_response(binding.response_schema, result)


def narrow_instance(schema: type[Schema], value: Schema) -> object:
    """``value`` as the declared ``schema``: itself when it is exactly that
    Schema (or its lazy projection), else what ``schema`` declares: its fields and
    computed values, nested values narrowed by its own field types. A ``schema``
    that decides its own ``dump()`` keeps the plain projection."""
    is_exact = type(value) is schema or (
        isinstance(value, _ProjectedItem)
        and value._projected_handle is not None
        and value._schema is schema
    )
    if is_exact:
        return value
    narrowed = core.dump_as(value, schema)
    return value.dump(include=schema.specs()) if narrowed is None else narrowed


def narrow_result(schema: type[Schema], value: object, exact: bool = False) -> object:
    """Shape a handler result declared ``list[S]``, ``Page[S]``, ``CursorPage[S]``
    or ``S | None`` (``schema`` is ``S``): strict subclasses of ``S`` are sent
    with the declared fields only. ``exact``: the validation pass already proved a
    list holds nothing but ``S``."""
    if exact:
        return value
    if isinstance(value, list):
        return narrow_items(schema, value)
    if isinstance(value, (ResultPage, CursorPage)):
        return narrow_page(schema, value)
    return narrow_instance(schema, value) if isinstance(value, schema) else value


def narrow_items(schema: type[Schema], items: list[Any]) -> list[Any]:
    """``items`` with every strict subclass of ``schema`` narrowed to its
    declared fields; the same list when every item is exactly ``schema``."""
    if (
        type(items) is not list
        and isinstance(items, _ProjectedList)
        and items._projected_handle is not None
    ):
        lazy = items._schema
        if lazy is schema or not issubclass(lazy, schema):
            return items  # pre-encoded as the declared Schema, or not ours to narrow
    if core.all_exact(items, schema):
        return items
    return [
        narrow_instance(schema, item) if isinstance(item, schema) else item
        for item in items
    ]


def narrow_page(
    schema: type[Schema], page: ResultPage[Any] | CursorPage[Any]
) -> ResultPage[Any] | CursorPage[Any]:
    """The same narrowing for the items of a page (a new page when any changed)."""
    if isinstance(page, _ProjectedPage) and page._projected_handle is not None:
        lazy = page._schema
        if lazy is schema or not issubclass(lazy, schema):
            return page
    narrowed = narrow_items(schema, page.items)
    if narrowed is page.items:
        return page
    if isinstance(page, ResultPage):
        return ResultPage(narrowed, page.page, page.page_size, page.total)
    return CursorPage(narrowed, page.next, page.previous, page.page_size)


def project_response(schema: type[Schema] | None, value: object) -> object:
    """Shape a handler result by its single declared response Schema.

    A Model is projected with ``from_model()``; an instance of a Schema subclass
    is sent with the declared Schema's fields only. Other values pass through.
    """
    if schema is None:
        return value
    if isinstance(value, schema):
        return narrow_instance(schema, value)
    if not isinstance(value, Model):
        return value
    source = schema.__derived_model__
    if source is not None and not isinstance(value, source):
        raise TypeError(
            f"{schema.__name__} is derived from {source.__name__}, "
            f"not {type(value).__name__}"
        )
    try:
        return schema.from_model(value)
    except ValidationError as exc:
        raise ResponseProjectionError(
            f"the {type(value).__name__} returned by the handler does not "
            f"satisfy {schema.__name__}"
        ) from exc
