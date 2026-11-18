"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import sys
from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, datetime, tzinfo
from functools import partial
from types import FrameType, ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from hinoki.api import API
from hinoki.core import Router
from hinoki.db import DatabaseModule
from hinoki.docsui import pages as docs_pages
from hinoki.errors import ErrorRegistrar, ErrorRegistry, RegistrationError
from hinoki.fields import UNSET, Unset
from hinoki.management import Command, _normalize_commands
from hinoki.modules import Module, order_modules
from hinoki.openapi import OpenAPIBuilder
from hinoki.pagination import DEFAULT_MAX_PAGE_SIZE, DEFAULT_PAGE_SIZE
from hinoki.parsers import (
    DEFAULT_PARSERS,
    FORM_PARSERS,
    UPLOAD_PARSERS,
    MultiPartParser,
    Parser,
    check_parsers,
)
from hinoki.project import manifest_declaring
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
from hinoki.rules import Phase, Project, RuleSet, failing
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.runtime.binding import Binding, bind, declares_form, declares_upload
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.lifespan import Hook
from hinoki.settings import (
    AdminSettings,
    AppSettings,
    DatabaseSettings,
    DocsSettings,
    Settings,
)
from hinoki.utils import Clock, _read_utc, utc_now


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
    item: object, timezone: tzinfo, settings: AdminSettings | None = None
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
        bind_settings(settings)


def bind_app_timezones(
    items: Sequence[object], timezone: tzinfo, settings: AdminSettings | None = None
) -> None:
    """Bind integrations after the app has validated all routes."""
    for item, _prefix in flatten(items):
        bind_app_timezone(item, timezone, settings)


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

    ``startup_url`` optionally reserves a public GET/HEAD endpoint that returns
    ``{"status": "ok"}`` after worker startup. It reports startup completion,
    not application, database, or event-loop health.
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
        startup_url: str | None = None,
        runtime: RuntimeConfig | None = None,
        title: str | None = None,
        version: str | None = None,
        openapi_url: str | None | Unset = UNSET,
        docs_url: str | None | Unset = UNSET,
        redoc_url: str | None | Unset = UNSET,
        rules: RuleSet | None = None,
        enforce_rules: bool = True,
        parsers: Sequence[Parser] | None = None,
        renderers: Sequence[Renderer] | None = None,
    ) -> None:
        if isinstance(settings, type) and issubclass(settings, Settings):
            settings = settings.load()  # a class: read its variables now
        self._startup_url = check_startup_url(startup_url)
        # An explicit argument wins over ``settings``, which wins over the default.
        built_in = settings if isinstance(settings, AppSettings) else None
        self._timezone = ZoneInfo(built_in.timezone) if built_in else UTC
        docs = built_in.docs if built_in else DocsSettings()
        self.settings = settings
        self._configure_runtime(built_in, runtime)
        self._configure_docs(docs, title, version, openapi_url, docs_url, redoc_url)
        self._configure_policy(built_in, rules, enforce_rules, parsers, renderers)
        self._initialize_registries(commands, on_startup, on_shutdown)
        # The declared form: ``Hinoki(include=[...], modules=[...])``.
        declared = [self.module_of(m) for m in modules]
        automatic = self.settings_database(declared)
        if automatic is not None or declared:
            self.install(*([automatic] if automatic else []), *declared)
        self._automatic_database: DatabaseModule | None = automatic
        if include is None:
            include = self._project_routes(sys._getframe(1))
        if include:
            self.include(*include)

    @staticmethod
    def _project_routes(caller: FrameType) -> list[Includable]:
        """Without ``include=``, the routes ``hinoki.toml`` declares, when the
        manifest names the module that builds this application."""
        manifest = manifest_declaring(
            caller.f_globals.get("__name__", ""), caller.f_globals.get("__file__")
        )
        return manifest.route_items() if manifest is not None else []

    def admin_settings(self) -> AdminSettings | None:
        """The app's ``admin`` settings group, or ``None`` without built-in settings."""
        return self.settings.admin if isinstance(self.settings, AppSettings) else None

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
        openapi_url: str | None | Unset,
        docs_url: str | None | Unset,
        redoc_url: str | None | Unset,
    ) -> None:
        self.title = title if title is not None else docs.title
        self.version = version if version is not None else docs.version
        # ``None`` disables a page even when the settings enable it; unset defers.
        openapi_url = (
            docs.openapi_url if isinstance(openapi_url, Unset) else openapi_url
        )
        docs_url = docs.docs_url if isinstance(docs_url, Unset) else docs_url
        redoc_url = docs.redoc_url if isinstance(redoc_url, Unset) else redoc_url
        self.openapi_url = openapi_url
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        docs_pages(openapi_url, docs_url, redoc_url)  # fail early on a bad combination

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

    def _initialize_registries(
        self,
        commands: Sequence[Command | type[Command]],
        on_startup: Sequence[Hook],
        on_shutdown: Sequence[Hook],
    ) -> None:
        self.errors = ErrorRegistry()
        self._routes: list[Route] = []
        self._bindings: list[Binding] = []
        self._modules: list[Module] = []
        self._automatic_database = None
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

    def module_of(self, module: Module | type[Module]) -> Module:
        """An instance as given, or a class built from this app's settings."""
        if isinstance(module, type):
            return module.from_settings(self.settings)
        return module

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

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
        pending, groups = self._collect_routes(items)
        pending = [self.with_defaults(route) for route in pending]
        bound = [bind(route) for route in pending]  # bad signatures fail here
        # CRUD row plans must be scoped to this app's resolved renderers and binding.
        from hinoki.runtime import rowplan  # noqa: PLC0415 - CRUD is optional here

        bound = [rowplan.with_row_wire(binding) for binding in bound]
        staged = self._stage_router(pending)
        bind_app_timezones(items, self._timezone, self.admin_settings())
        self._router = staged
        self._routes.extend(pending)
        self._bindings.extend(bound)
        for group in groups:
            group._include()
        self._included_groups.update(groups)

    def _collect_routes(
        self, items: tuple[object, ...]
    ) -> tuple[list[Route], list[RouteGroup]]:
        pending: list[Route] = []
        groups: list[RouteGroup] = []
        for item, prefix in flatten(items):
            if isinstance(item, RouteGroup):
                if item in self._included_groups or item in groups:
                    raise RegistrationError("a RouteGroup can be included only once")
                groups.append(item)
            bind_mount = getattr(item, "bind_mount", None)
            if callable(bind_mount):
                bind_mount(prefix)  # e.g. the admin builds its links from it
            found = self.routes_of(item)
            pending.extend(
                dataclasses.replace(
                    route, path=prefix + ("" if route.path == "/" else route.path)
                )
                if prefix
                else route
                for route in found
            )
        return pending, groups

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
        if declares_form(route) and not any(p.forms for p in parsers):
            return FORM_PARSERS
        return parsers

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
                changes["pagination"] = route.pagination.resolved(
                    *self.pagination_defaults
                )
            except ValueError as exc:
                raise RegistrationError(f"{route.name}: pagination {exc}") from exc
        return dataclasses.replace(route, **changes) if changes else route

    def install(self, *modules: Module) -> None:
        """Install modules and include their APIs.

        The whole graph (names, requirements, cycles) is validated here, so a
        broken graph fails in the factory, not in a worker. Nothing opens.
        """
        self._check_mutable()
        installed = self._modules
        automatic = self._automatic_database
        replacing_automatic = automatic is not None and any(
            isinstance(module, DatabaseModule) for module in modules
        )
        if replacing_automatic:
            # Validate and include first; a failed installation must preserve the
            # settings-provided module and allow a later replacement attempt.
            installed = [module for module in installed if module is not automatic]
        order_modules([*installed, *modules])
        self.include(*(api for module in modules for api in module.apis))
        self._modules[:] = [*installed, *modules]
        if replacing_automatic:
            self._automatic_database = None

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
        """The OpenAPI 3.1 document for the registered routes."""
        return OpenAPIBuilder(self, self.title, self.version).build()

    def asgi(self) -> ASGIAdapter:
        """The ASGI adapter for this worker; builds and validates bindings once."""
        if self._adapter is None:
            adapter = ASGIAdapter(self)  # bad signatures fail here
            self.enforce()
            self._adapter = adapter
        return self._adapter

    def enforce(self) -> None:
        """Fail with every startup-rule violation when rules are enforced. With
        ``enforce_rules=False`` the diagnostics are only logged."""
        if self.rules is None:
            return
        diagnostics = self.rules.run(Project(self), Phase.STARTUP)
        broken = failing(diagnostics)
        if not broken:
            return
        report = "\n".join(d.text() for d in broken)
        if self.enforce_rules:
            raise RegistrationError(f"{len(broken)} rule violation(s):\n{report}")
        log.warning("%d rule violation(s):\n%s", len(broken), report)

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
