"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import MutableMapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from hinoki.api import API
from hinoki.core import Router
from hinoki.docsui import pages as docs_pages
from hinoki.errors import RegistrationError
from hinoki.fields import UNSET, Unset
from hinoki.httperrors import ErrorRegistry
from hinoki.management import Command, _normalize_commands
from hinoki.modules import Module, order_modules
from hinoki.openapi import OpenAPIBuilder
from hinoki.parsers import DEFAULT_PARSERS, Parser, check_parsers
from hinoki.renderers import DEFAULT_RENDERERS, Renderer, check_renderers
from hinoki.routing import Found, MethodNotAllowed, Route, RouteGroup, route_of
from hinoki.rules import Phase, Project, RuleSet, failing
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.runtime.binding import Binding, bind
from hinoki.runtime.config import RuntimeConfig
from hinoki.settings import DocsSettings, FrameworkSettings, Settings
from hinoki.utils import Clock, _read_utc, utc_now

log = logging.getLogger("hinoki")


class Hinoki:
    """A Hinoki application.

    Registering routes builds the native router; nothing connects to
    anything, so factories returning ``Hinoki`` are safe to import and call.
    The instance is itself an ASGI callable (``granian --interface asgi
    module:app``); the first call freezes the declarations.
    """

    def __init__(
        self,
        *,
        settings: object | None = None,
        include: Sequence[object] = (),
        modules: Sequence[Module | type[Module]] = (),
        commands: Sequence[Command | type[Command]] = (),
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
        # An explicit argument wins over ``settings``, which wins over the default.
        built_in = settings if isinstance(settings, FrameworkSettings) else None
        self._timezone = ZoneInfo(built_in.timezone) if built_in else UTC
        docs = built_in.docs if built_in else DocsSettings()
        self.settings = settings
        self.runtime = runtime or (
            built_in.runtime_config() if built_in else RuntimeConfig()
        )
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
        self.rules = rules
        self.enforce_rules = enforce_rules
        # Application-wide defaults for actions that declare no parsers/renderers.
        self.default_parsers = None if parsers is None else check_parsers(parsers)
        self.default_renderers = (
            None if renderers is None else check_renderers(renderers)
        )
        self.errors = ErrorRegistry()
        self._routes: list[Route] = []
        self._bindings: list[Binding] = []
        self._modules: list[Module] = []
        self._commands = _normalize_commands(commands)
        self._included_groups: set[RouteGroup] = set()
        self._router = Router()
        self._adapter: ASGIAdapter | None = None
        # The declared form: ``Hinoki(include=[...], modules=[...])``.
        if modules:
            self.install(*(self.module_of(m) for m in modules))
        if include:
            self.include(*include)

    def now(self, clock: Clock = utc_now) -> datetime:
        """Return an aware datetime in this application's configured timezone."""
        return _read_utc(clock).astimezone(self._timezone)

    def module_of(self, module: Module | type[Module]) -> Module:
        """An instance as given, or a class built from this app's settings."""
        if isinstance(module, type):
            return module.from_settings(self.settings)
        return module

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

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

    def include(self, *items: object) -> None:
        """Register decorated functions, ``API`` subclasses and objects that expose
        an ``api()`` (the admin).

        All-or-nothing per call: on conflict the application is unchanged.
        """
        self._check_mutable()
        pending: list[Route] = []
        groups: list[RouteGroup] = []
        for item in items:
            if isinstance(item, RouteGroup):
                if item in self._included_groups or item in groups:
                    raise RegistrationError("a RouteGroup can be included only once")
                groups.append(item)
            pending.extend(self.routes_of(item))
        pending = [self.with_defaults(route) for route in pending]
        bound = [bind(route) for route in pending]  # bad signatures fail here
        staged = Router()
        for index, route in enumerate(self._routes):
            staged.add(route.method, route.path, index)
        for offset, route in enumerate(pending):
            try:
                staged.add(route.method, route.path, len(self._routes) + offset)
            except ValueError as exc:
                raise RegistrationError(f"{route.name}: {exc}") from exc
        self._router = staged
        self._routes.extend(pending)
        self._bindings.extend(bound)
        for group in groups:
            group._include()
        self._included_groups.update(groups)

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
            raise RegistrationError(
                f"{item!r} is neither an API class nor a decorated function"
            )
        if route.replaces:
            raise RegistrationError(
                f"{route.name}: replaces=... only works inside an API class"
            )
        return [route]

    def with_defaults(self, route: Route) -> Route:
        """The route with this application's default parsers/renderers filled in
        where the action did not choose its own."""
        changes: dict[str, Any] = {}
        if self.default_parsers is not None and route.parsers is DEFAULT_PARSERS:
            changes["parsers"] = self.default_parsers
        if self.default_renderers is not None and route.renderers is DEFAULT_RENDERERS:
            changes["renderers"] = self.default_renderers
        return dataclasses.replace(route, **changes) if changes else route

    def install(self, *modules: Module) -> None:
        """Install modules and include their APIs.

        The whole graph (names, requirements, cycles) is validated here, so a
        broken graph fails in the factory, not in a worker. Nothing opens.
        """
        self._check_mutable()
        order_modules([*self._modules, *modules])
        self.include(*(api for module in modules for api in module.apis))
        self._modules.extend(modules)

    def lookup(self, method: str, path: str) -> Found | MethodNotAllowed | None:
        """Distinguish a match, a wrong method and an unknown path."""
        raw = self._router.find(method, path)
        if raw is None:
            return None
        head, tail = raw
        if isinstance(head, int) and isinstance(tail, dict):
            return Found(head, tail)
        if isinstance(head, str) and isinstance(tail, list):
            return MethodNotAllowed(tail)
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

    def _check_mutable(self) -> None:
        if self._adapter is not None:
            raise RegistrationError("application is already serving; it is frozen")
