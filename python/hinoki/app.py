"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import sys
from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, datetime, tzinfo
from functools import partial
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, Literal
from zoneinfo import ZoneInfo

from hinoki.api import API
from hinoki.core import Router
from hinoki.db import DatabaseModule
from hinoki.docsui import check_llms_url
from hinoki.docsui import pages as docs_pages
from hinoki.errors import ErrorRegistrar, ErrorRegistry, RegistrationError
from hinoki.events import EventBus, Handlers
from hinoki.fields import UNSET, Unset
from hinoki.http.middleware import Middleware, check_middleware
from hinoki.mail import EmailModule, TemplateBase
from hinoki.management import Command, _normalize_commands
from hinoki.modules import Module, order_modules
from hinoki.openapi import SPEC_VERSIONS, OpenAPIBuilder, documented
from hinoki.pagination import (
    DEFAULT_MAX_PAGE_SIZE,
    DEFAULT_PAGE_SIZE,
    CursorPagination,
    Pagination,
)
from hinoki.parsers import (
    DEFAULT_PARSERS,
    SOURCE_DEFAULTS,
    UPLOAD_PARSERS,
    MultiPartParser,
    Parser,
    check_parsers,
)
from hinoki.project import ProjectManifest, manifest_declaring
from hinoki.renderers import DEFAULT_RENDERERS, Renderer, check_renderers
from hinoki.routing import (
    Found,
    Includable,
    MethodNotAllowed,
    Mount,
    Route,
    RouteGroup,
    route_of,
)
from hinoki.rules import Phase, Project, RuleSet, failing, qualified
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.runtime.binding import (
    Binding,
    accepts_source,
    bind,
    declared_source,
    declares_upload,
)
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.lifespan import Hook
from hinoki.settings import (
    AdminSettings,
    AppSettings,
    DatabaseSettings,
    DocsSettings,
    EmailSettings,
    Settings,
)
from hinoki.utils import Clock, _read_utc, utc_now, utc_today
from hinoki.waivers import EXPIRED, WaiverError, apply_waivers, read_waivers
from hinoki.websocket import WebSocketRoute, websocket_of


def check_startup_url(value: str | None) -> str | None:
    """Validate an opt-in, literal ASCII path for the startup probe."""
    if value is None:
        return None
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-._~"
    )
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or "//" in value
        or any(char not in allowed for char in value)
        or any(part in {".", ".."} for part in value.split("/")[1:])
    ):
        raise RegistrationError(
            "startup_url must be a literal ASCII path without query, fragment, "
            "parameters, escapes, or dot segments"
        )
    return value


log = logging.getLogger("hinoki")


def bind_app_timezone(
    item: object,
    timezone: tzinfo,
    settings: AdminSettings | None = None,
    env_prefix: str = "",
) -> None:
    """Bind the timezone and server secret of the app to an included admin,
    whether it is included as an object or as an API class."""
    admin = (
        item
        if callable(getattr(item, "bind_timezone", None))
        else getattr(item, "admin", None)
    )
    bind_timezone = getattr(admin, "bind_timezone", None)
    if callable(bind_timezone):
        bind_timezone(timezone)
    bind_settings = getattr(admin, "bind_settings", None)
    if callable(bind_settings):
        bind_settings(settings, env_prefix)


def bind_app_timezones(
    items: Sequence[object],
    timezone: tzinfo,
    settings: AdminSettings | None = None,
    env_prefix: str = "",
) -> None:
    """Bind integrations after the app has validated all routes."""
    for item, _prefix in flatten(items):
        bind_app_timezone(item, timezone, settings, env_prefix)


def check_hooks(option: str, hooks: Sequence[Hook]) -> tuple[Hook, ...]:
    """Validate and freeze an explicit list or tuple of hook callables."""
    if not isinstance(hooks, (list, tuple)):
        raise RegistrationError(f"{option} takes a list of functions")
    frozen = tuple(hooks)
    for hook in frozen:
        if not callable(hook):
            raise RegistrationError(f"{option}: {hook!r} is not a function")
        target = hook
        while True:
            if isinstance(target, partial):
                target = target.func
                continue
            call = inspect.getattr_static(type(target), "__call__", None)
            if inspect.isasyncgenfunction(target) or inspect.isasyncgenfunction(call):
                raise RegistrationError(
                    f"{option}: async-generator hooks are not supported"
                )
            break
    return frozen


class Hinoki:
    """A Hinoki application.

    Registering routes builds the native router; nothing connects to
    anything, so factories returning ``Hinoki`` are safe to import and call.
    The instance is itself an ASGI callable (``granian --interface asgi
    module:app``); the first call freezes the declarations.

    ``on_startup`` and ``on_shutdown`` list functions taking the worker's
    ``ModuleContext`` (``async def`` or ``def``). Startup hooks run once per
    worker after the modules have started; shutdown hooks run before they are
    closed, and only after a startup that completed.

    ``events`` lists the ``Handlers`` collections whose typed event handlers
    run, in this order (see ``hinoki.events``); an unlisted one does nothing.

    ``email_templates`` lists the ``mail.Template`` objects the application
    sends. They go to the email module the ``email: EmailSettings`` field
    implies; with your own ``EmailModule`` pass them there instead. File
    templates are read and checked when each worker starts.

    ``startup_url`` optionally reserves a public GET/HEAD endpoint that returns
    ``{"status": "ok"}`` after worker startup. It reports startup completion,
    not application, database, or event-loop health.

    ``waivers`` names the owner's waiver file applied to the startup rules; it
    must exist. Without it, ``hinoki-rules.toml`` at the project root is used.
    """

    def __init__(
        self,
        *,
        settings: object | None = None,
        include: Sequence[Includable] | None = None,
        modules: Sequence[Module | type[Module]] = (),
        commands: Sequence[Command | type[Command]] = (),
        on_startup: Sequence[Hook] = (),
        on_shutdown: Sequence[Hook] = (),
        events: Sequence[Handlers] | None = None,
        email_templates: Sequence[TemplateBase] = (),
        startup_url: str | None = None,
        runtime: RuntimeConfig | None = None,
        title: str | None = None,
        version: str | None = None,
        openapi_url: str | Unset | None = UNSET,
        docs_url: str | Unset | None = UNSET,
        redoc_url: str | Unset | None = UNSET,
        llms_url: str | Unset | None = UNSET,
        openapi_version: Literal["3.1", "3.2"] | None = None,
        rules: RuleSet | None = None,
        enforce_rules: bool = True,
        waivers: str | Path | None = None,
        parsers: Sequence[Parser] | None = None,
        renderers: Sequence[Renderer] | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> None:
        include, events, middleware, rules = self._manifest_defaults(
            sys._getframe(1), include, events, middleware, rules
        )
        if isinstance(settings, type) and issubclass(settings, Settings):
            settings = settings.load()  # a class: read its variables now
        self._startup_url = check_startup_url(startup_url)
        # An explicit argument wins over ``settings``, which wins over the default.
        built_in = settings if isinstance(settings, AppSettings) else None
        self._timezone = ZoneInfo(built_in.timezone) if built_in else UTC
        docs = built_in.docs if built_in else DocsSettings()
        self.settings = settings
        self.middleware = check_middleware(middleware)
        self._configure_runtime(built_in, runtime)
        self._configure_docs(
            docs,
            title,
            version,
            openapi_url,
            docs_url,
            redoc_url,
            llms_url=llms_url,
            openapi_version=openapi_version,
        )
        self._configure_policy(built_in, rules, enforce_rules, parsers, renderers)
        # An explicit waiver file wins over the project's ``hinoki-rules.toml``.
        self.waiver_file: Path | None = None if waivers is None else Path(waivers)
        self._initialize_registries(commands, on_startup, on_shutdown)
        self._events = EventBus(events, built_in.events if built_in else None)
        # The declared form: ``Hinoki(include=[...], modules=[...])``.
        declared = [self.module_of(m) for m in modules]
        automatic = self.settings_database(declared)
        automatic_email = self.settings_email(declared, email_templates)
        implied = [m for m in (automatic, automatic_email) if m is not None]
        if implied or declared:
            self.install(*implied, *declared)
        self._automatic_database: DatabaseModule | None = automatic
        self._automatic_email: EmailModule | None = automatic_email
        if include:
            self.include(*include)

    @classmethod
    def _manifest_defaults(
        cls,
        caller: FrameType,
        include: Sequence[Includable] | None,
        events: Sequence[Handlers] | None,
        middleware: Sequence[Middleware] | None,
        rules: RuleSet | None,
    ) -> tuple[
        Sequence[Includable],
        Sequence[Handlers],
        Sequence[Middleware],
        RuleSet | None,
    ]:
        """Fill each of ``include=``, ``events=``, ``middleware=`` and ``rules=``
        that was left out from ``hinoki.toml``; an explicit value, even empty,
        is kept. ``rules`` stays ``None`` when the manifest lists none."""
        manifest = (
            cls._project_manifest(caller)
            if any(value is None for value in (include, events, middleware, rules))
            else None
        )
        if manifest is None:
            return include or (), events or (), middleware or (), rules
        return (
            manifest.route_items() if include is None else include,
            manifest.event_handlers() if events is None else events,
            manifest.middleware_list() if middleware is None else middleware,
            manifest.rule_set() if rules is None and manifest.rules else rules,
        )

    @staticmethod
    def _project_manifest(caller: FrameType) -> ProjectManifest | None:
        """Without ``include=``, ``events=``, ``middleware=`` or ``rules=``, what
        ``hinoki.toml`` declares, when the manifest names the module that builds
        this application."""
        return manifest_declaring(
            caller.f_globals.get("__name__", ""), caller.f_globals.get("__file__")
        )

    def admin_settings(self) -> AdminSettings | None:
        """The app's ``admin`` settings group, or ``None`` without built-in settings."""
        return self.settings.admin if isinstance(self.settings, AppSettings) else None

    def _env_prefix(self) -> str:
        """The environment prefix declared by the app's settings class."""
        if isinstance(self.settings, AppSettings):
            return type(self.settings).__env_prefix__
        return ""

    def _configure_runtime(
        self, built_in: AppSettings | None, runtime: RuntimeConfig | None
    ) -> None:
        self.runtime = runtime or (
            built_in.runtime_config() if built_in else RuntimeConfig()
        )

    def _configure_docs(
        self,
        docs: DocsSettings,
        title: str | None,
        version: str | None,
        openapi_url: str | Unset | None,
        docs_url: str | Unset | None,
        redoc_url: str | Unset | None,
        *,
        llms_url: str | Unset | None,
        openapi_version: str | None,
    ) -> None:
        self.title = title if title is not None else docs.title
        self.version = version if version is not None else docs.version
        self.openapi_version = (
            openapi_version if openapi_version is not None else docs.openapi_version
        )
        if self.openapi_version not in SPEC_VERSIONS:
            raise ValueError(
                f"openapi_version must be one of {sorted(SPEC_VERSIONS)}, "
                f"not {self.openapi_version!r}"
            )
        # ``None`` disables a page even when the settings enable it; unset defers.
        openapi_url = (
            docs.openapi_url if isinstance(openapi_url, Unset) else openapi_url
        )
        docs_url = docs.docs_url if isinstance(docs_url, Unset) else docs_url
        redoc_url = docs.redoc_url if isinstance(redoc_url, Unset) else redoc_url
        self.openapi_url = openapi_url
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.llms_url = docs.llms_url if isinstance(llms_url, Unset) else llms_url
        docs_pages(openapi_url, docs_url, redoc_url)  # fail early on a bad combination
        check_llms_url(self.llms_url, openapi_url, docs_url, redoc_url)

    def _configure_policy(
        self,
        built_in: AppSettings | None,
        rules: RuleSet | None,
        enforce_rules: bool,
        parsers: Sequence[Parser] | None,
        renderers: Sequence[Renderer] | None,
    ) -> None:
        self.rules = rules
        self.enforce_rules = enforce_rules
        # Application-wide defaults for actions that declare no parsers/renderers.
        self.default_parsers = None if parsers is None else check_parsers(parsers)
        self.default_renderers = (
            None if renderers is None else check_renderers(renderers)
        )
        self.pagination_defaults = (
            (built_in.pagination.page_size, built_in.pagination.max_page_size)
            if built_in
            else (DEFAULT_PAGE_SIZE, DEFAULT_MAX_PAGE_SIZE)
        )
        cursor_secret = built_in.pagination.cursor_secret if built_in else None
        self.cursor_defaults = (
            None if cursor_secret is None else cursor_secret.reveal(),
            built_in.pagination.cursor_ttl_seconds if built_in else None,
        )

    def _initialize_registries(
        self,
        commands: Sequence[Command | type[Command]],
        on_startup: Sequence[Hook],
        on_shutdown: Sequence[Hook],
    ) -> None:
        self.errors = ErrorRegistry()
        self._routes: list[Route] = []
        self._websockets: list[WebSocketRoute] = []
        self._ws_router = Router()
        self._bindings: list[Binding] = []
        self._modules: list[Module] = []
        self._automatic_database = None
        self._automatic_email = None
        self._commands = _normalize_commands(commands)
        self._on_startup = check_hooks("on_startup", on_startup)
        self._on_shutdown = check_hooks("on_shutdown", on_shutdown)
        self._included_groups: set[RouteGroup] = set()
        self._router = Router()
        self._adapter: ASGIAdapter | None = None

    def now(self, clock: Clock = utc_now) -> datetime:
        """Return an aware datetime in this application's configured timezone."""
        return _read_utc(clock).astimezone(self._timezone)

    def settings_database(self, declared: list[Module]) -> DatabaseModule | None:
        """The database module implied by a ``database: DatabaseSettings`` field
        in the settings, unless the application declares its own."""
        group = getattr(self.settings, "database", None)
        if not isinstance(group, DatabaseSettings):
            return None
        if any(
            isinstance(module, DatabaseModule) or module.name == DatabaseModule.name
            for module in declared
        ):
            return None
        return DatabaseModule.from_settings(self.settings)

    def settings_email(
        self, declared: list[Module], templates: Sequence[TemplateBase] = ()
    ) -> EmailModule | None:
        """The email module implied by an ``email: EmailSettings`` field in the
        settings, unless the application declares its own. ``templates`` go to
        the implied module; they are refused when there is none."""
        group = getattr(self.settings, "email", None)
        own = any(
            isinstance(module, EmailModule) or module.name == EmailModule.name
            for module in declared
        )
        if templates and own:
            raise RegistrationError(
                "email_templates cannot reach your own EmailModule: "
                "pass them as EmailModule(settings, templates=[...])"
            )
        if not isinstance(group, EmailSettings):
            if templates:
                raise RegistrationError(
                    "email_templates needs an `email: EmailSettings` field in the "
                    "settings, or an EmailModule(settings, templates=[...])"
                )
            return None
        if own:
            return None
        return EmailModule.from_settings(self.settings, templates)

    def module_of(self, module: Module | type[Module]) -> Module:
        """An instance as given, or a class built from this app's settings."""
        if isinstance(module, type):
            return module.from_settings(self.settings)
        return module

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    @property
    def websockets(self) -> tuple[WebSocketRoute, ...]:
        return tuple(self._websockets)

    @property
    def startup_url(self) -> str | None:
        """The fixed startup-probe path, if one was configured."""
        return self._startup_url

    @property
    def bindings(self) -> tuple[Binding, ...]:
        return tuple(self._bindings)

    @property
    def modules(self) -> tuple[Module, ...]:
        return tuple(self._modules)

    @property
    def commands(self) -> tuple[Command, ...]:
        """The app's explicitly registered management commands."""
        return self._commands

    @property
    def events(self) -> EventBus:
        """The typed event handlers listed in ``events=``, fixed at construction."""
        return self._events

    @property
    def on_startup(self) -> tuple[Hook, ...]:
        """Startup hooks, fixed when the application is constructed."""
        return self._on_startup

    @property
    def on_shutdown(self) -> tuple[Hook, ...]:
        """Shutdown hooks, fixed when the application is constructed."""
        return self._on_shutdown

    def include(self, *items: Includable) -> None:
        """Register decorated functions, ``API`` subclasses and objects that expose
        an ``api()`` (the admin). Lists and tuples of those are flattened, so a
        feature package can export one ``routes`` list, and ``Mount(prefix, ...)``
        places a feature under a path chosen here.

        All-or-nothing per call: on conflict the application is unchanged.
        """
        self._check_mutable()
        pending, groups, sockets = self._collect_routes(items)
        refuse_shared_names([*self._routes, *pending])
        ws_router = self._stage_websockets(sockets, pending)
        refuse_duplicate_operation_ids([*self._routes, *pending])
        pending = [self.with_defaults(route) for route in pending]
        bound = [bind(route) for route in pending]  # bad signatures fail here
        # CRUD row plans must be scoped to this app's resolved renderers and binding.
        from hinoki.runtime import rowplan  # noqa: PLC0415 - CRUD is optional here

        bound = [rowplan.with_row_wire(binding) for binding in bound]
        staged = self._stage_router(pending)
        bind_app_timezones(
            items, self._timezone, self.admin_settings(), self._env_prefix()
        )
        self._router = staged
        self._ws_router = ws_router
        self._routes.extend(pending)
        self._websockets.extend(sockets)
        self._bindings.extend(bound)
        for group in groups:
            group._include()
        self._included_groups.update(groups)

    def _stage_websockets(
        self, sockets: list[WebSocketRoute], pending: list[Route]
    ) -> Router:
        """A router with the WebSocket routes, refusing a path an HTTP ``GET``
        route also takes: the handshake is a ``GET``, so one would hide the other."""
        staged = Router()
        for index, found in enumerate([*self._websockets, *sockets]):
            try:
                staged.add("GET", found.path, index)
            except ValueError as exc:
                raise RegistrationError(f"{found.name}: {exc}") from exc
        taken = {
            route.path for route in [*self._routes, *pending] if route.method == "GET"
        }
        for found in [*self._websockets, *sockets]:
            if found.path in taken:
                raise RegistrationError(
                    f"{found.name}: {found.path} is also an HTTP GET route"
                )
        return staged

    def lookup_websocket(
        self, path: str
    ) -> tuple[WebSocketRoute, dict[str, str]] | None:
        """The WebSocket action for a path, with its path parameters."""
        raw = self._ws_router.find("GET", path)
        if raw is None:
            return None
        head, tail = raw
        if isinstance(head, int) and isinstance(tail, dict):
            return self._websockets[head], tail
        return None  # pragma: no cover - the router only stores (int, dict) pairs

    def _collect_routes(
        self, items: tuple[object, ...]
    ) -> tuple[list[Route], list[RouteGroup], list[WebSocketRoute]]:
        pending: list[Route] = []
        groups: list[RouteGroup] = []
        sockets: list[WebSocketRoute] = []
        for item, prefix in flatten(items):
            declared = websocket_of(item)
            if declared is not None:
                sockets.append(self._mounted(declared, prefix).resolved())
                continue
            if isinstance(item, type) and issubclass(item, API):
                sockets.extend(
                    self._mounted(found, prefix).resolved()
                    for found in item.websockets()
                )
            if isinstance(item, RouteGroup):
                if item in self._included_groups or item in groups:
                    raise RegistrationError("a RouteGroup can be included only once")
                groups.append(item)
            bind_mount = getattr(item, "bind_mount", None)
            if callable(bind_mount):
                bind_mount(prefix)  # e.g. the admin builds its links from it
            found = self.routes_of(item)
            pending.extend(self._mounted(route, prefix) for route in found)
        return pending, groups, sockets

    @staticmethod
    def _mounted[R: (Route, WebSocketRoute)](route: R, prefix: str) -> R:
        if not prefix:
            return route
        path = prefix + ("" if route.path == "/" else route.path)
        return dataclasses.replace(route, path=path)

    def _stage_router(self, pending: list[Route]) -> Router:
        staged = Router()
        for index, route in enumerate(self._routes):
            staged.add(route.method, route.path, index)
        for offset, route in enumerate(pending):
            try:
                staged.add(route.method, route.path, len(self._routes) + offset)
            except ValueError as exc:
                raise RegistrationError(f"{route.name}: {exc}") from exc
        return staged

    @staticmethod
    def routes_of(item: object) -> list[Route]:
        """The routes an item declares: an API class, a decorated function, or an
        object exposing ``api()`` (the admin)."""
        exposed = getattr(item, "api", None)
        if not isinstance(item, type) and callable(exposed):
            item = exposed()
        if isinstance(item, RouteGroup):
            if not item.handlers:
                raise RegistrationError("a RouteGroup must declare at least one action")
            routes = [route_of(handler) for handler in item.handlers]
            if any(route is None for route in routes):
                raise RegistrationError("a RouteGroup contains an undecorated action")
            group_routes = [route for route in routes if route is not None]
            replaced = next((route for route in group_routes if route.replaces), None)
            if replaced is not None:
                raise RegistrationError(
                    f"{replaced.name}: replaces=... only works inside an API class"
                )
            return group_routes
        if isinstance(item, type) and issubclass(item, API):
            return list(item.routes())
        route = route_of(item)
        if route is None:
            hint = (
                "; export a list of its routes and include that"
                if isinstance(item, ModuleType)
                else ""
            )
            raise RegistrationError(
                f"{item!r} is neither an API class nor a decorated function{hint}"
            )
        if route.replaces:
            raise RegistrationError(
                f"{route.name}: replaces=... only works inside an API class"
            )
        return [route]

    def inherited_parsers(self, route: Route) -> tuple[Parser, ...]:
        """Parsers for an action without its own: the application's, unless
        an upload action needs multipart and they do not include it."""
        parsers = self.default_parsers or route.parsers
        has_multipart = any(isinstance(item, MultiPartParser) for item in parsers)
        if not has_multipart and declares_upload(route):
            return UPLOAD_PARSERS
        source = declared_source(route)
        if source is None:
            return parsers
        usable = tuple(p for p in parsers if accepts_source(source, p))
        return usable or SOURCE_DEFAULTS[source.kind]

    def with_defaults(self, route: Route) -> Route:
        """The route with this application's default parsers/renderers filled in
        where the action did not choose its own."""
        changes: dict[str, Any] = {}
        if route.parsers is DEFAULT_PARSERS:
            parsers = self.inherited_parsers(route)
            if parsers is not route.parsers:
                changes["parsers"] = parsers
        if self.default_renderers is not None and route.renderers is DEFAULT_RENDERERS:
            changes["renderers"] = self.default_renderers
        if route.pagination is not None:
            try:
                changes["pagination"] = self.resolved_pagination(route.pagination)
            except ValueError as exc:
                raise RegistrationError(f"{route.name}: pagination {exc}") from exc
        if self.middleware:
            changes["middleware"] = (*self.middleware, *route.middleware)
        return dataclasses.replace(route, **changes) if changes else route

    def resolved_pagination(self, strategy: Pagination) -> Pagination:
        if isinstance(strategy, CursorPagination):
            return strategy.resolved(*self.pagination_defaults, *self.cursor_defaults)
        return strategy.resolved(*self.pagination_defaults)

    def install(self, *modules: Module) -> None:
        """Install modules and include their APIs.

        The whole graph (names, requirements, cycles) is validated here, so a
        broken graph fails in the factory, not in a worker. Nothing opens.
        """
        self._check_mutable()
        installed = self._modules
        replaced = [
            automatic
            for automatic in (self._automatic_database, self._automatic_email)
            if automatic is not None
            and any(isinstance(module, type(automatic)) for module in modules)
        ]
        # Validate and include first; a failed installation must preserve the
        # settings-provided modules and allow a later replacement attempt.
        installed = [module for module in installed if module not in replaced]
        order_modules([*installed, *modules])
        self.include(*(api for module in modules for api in module.apis))
        self._modules[:] = [*installed, *modules]
        if self._automatic_database in replaced:
            self._automatic_database = None
        if self._automatic_email in replaced:
            self._automatic_email = None

    def lookup(self, method: str, path: str) -> Found | MethodNotAllowed | None:
        """Distinguish a match, a wrong method and an unknown path.

        HEAD resolves through a matching GET route. Method-not-allowed results
        advertise HEAD whenever GET is available for the path.
        """
        raw = self._router.find(method, path)
        if raw is None:
            return None
        head, tail = raw
        if isinstance(head, int) and isinstance(tail, dict):
            return Found(head, tail)
        if isinstance(head, str) and isinstance(tail, list):
            # Method names are case-sensitive: only an exact HEAD borrows GET,
            # which is also the only spelling the adapter strips the body for.
            if method == "HEAD" and "GET" in tail:
                return self.lookup("GET", path)
            allowed = [*tail, "HEAD"] if "GET" in tail else tail
            return MethodNotAllowed(sorted(allowed))
        raise RuntimeError("unexpected router answer")

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        """Find the action for a request line; ``None`` if unknown or the
        method is not allowed (use ``lookup`` to tell them apart)."""
        found = self.lookup(method, path)
        if isinstance(found, Found):
            return self._routes[found.action], found.params
        return None

    def openapi(self) -> dict[str, Any]:
        """The OpenAPI document (3.2 by default, 3.1 with ``openapi_version``)."""
        return OpenAPIBuilder(
            self, self.title, self.version, self.openapi_version
        ).build()

    def asgi(self) -> ASGIAdapter:
        """The ASGI adapter for this worker; builds and validates bindings once."""
        if self._adapter is None:
            adapter = ASGIAdapter(self)  # bad signatures fail here
            self.enforce()
            self._adapter = adapter
        return self._adapter

    def enforce(self) -> None:
        """Fail with every startup-rule violation when rules are enforced. With
        ``enforce_rules=False`` the diagnostics are only logged. The owner's
        waivers apply as in ``hinoki check``: ``waivers=`` when given, else the
        project's ``hinoki-rules.toml``. An invalid waiver file always fails."""
        if self.rules is None:
            return
        try:
            waivers = read_waivers(self.waiver_file)
        except WaiverError as exc:
            raise RegistrationError(f"invalid waivers: {exc}") from exc
        diagnostics = apply_waivers(
            self.rules.run(Project(self), Phase.STARTUP), waivers, utc_today()
        )
        # Only startup rules ran, so an unused waiver may belong to a static rule.
        expired = [d for d in diagnostics if d.rule_id == EXPIRED]
        broken = failing(diagnostics)
        report = "\n".join(d.text() for d in (*broken, *expired))
        if self.enforce_rules and broken:
            raise RegistrationError(f"{len(broken)} rule violation(s):\n{report}")
        if report:
            log.warning(
                "%d rule violation(s), %d expired waiver(s):\n%s",
                len(broken),
                len(expired),
                report,
            )

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        try:
            adapter = self.asgi()
        except RegistrationError as exc:
            if scope["type"] != "lifespan":
                raise
            await receive()
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
            return
        await adapter(scope, receive, send)

    def error_handler(self, kind: type[Exception]) -> ErrorRegistrar:
        """Decorator: answer ``kind`` with a function of the exception that returns
        the ``APIError`` to send. It returns the function unchanged.

            @app.error_handler(stripe.CardError)
            def card_error(error: stripe.CardError) -> errors.APIError:
                return errors.APIError(error.user_message, status=402)

        Errors that already are ``APIError`` subclasses need no handler.
        """
        self._check_mutable()
        return self.errors.handle(kind)

    def _check_mutable(self) -> None:
        if self._adapter is not None:
            raise RegistrationError("application is already serving; it is frozen")


def refuse_duplicate_operation_ids(routes: Sequence[Route]) -> None:
    """An ``operation_id`` names one operation in the OpenAPI document."""
    seen: dict[str, Route] = {}
    for route in routes:
        if not route.operation_id or not documented(route):
            continue
        first = seen.setdefault(route.operation_id, route)
        if first is not route:
            raise RegistrationError(
                f"operation_id {route.operation_id!r} is declared by "
                f"{first.name} and {route.name}; it must be unique"
            )


def refuse_shared_names(routes: Sequence[Route]) -> None:
    """Two different API classes must not give an action the same qualified name:
    one rule pattern, waiver or MCP tool would then silently cover both. The same
    class mounted twice keeps one name."""
    owners: dict[str, tuple[type, str]] = {}
    for route in routes:
        if route.owner is None:
            continue
        name = qualified(route)
        first = owners.setdefault(name, (route.owner, route.path))
        if first[0] is not route.owner:
            raise RegistrationError(
                f"{name} is declared by two classes ({first[1]} and {route.path}); "
                "give one a distinct class name, or crud(..., name=...)"
            )


def flatten(items: Sequence[object], prefix: str = "") -> Iterator[tuple[object, str]]:
    """``(item, prefix)`` for the items of ``include``: nested lists and tuples are
    spread in place and a ``Mount`` adds its prefix to what it holds."""
    for item in items:
        if isinstance(item, Mount):
            yield from flatten(item.items, prefix + item.prefix)
        elif isinstance(item, list | tuple):
            yield from flatten(item, prefix)
        else:
            yield item, prefix
