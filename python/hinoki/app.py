"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

from hinoki.api import API
from hinoki.docsui import pages as docs_pages
from hinoki.errors import RegistrationError
from hinoki.httperrors import ErrorRegistry
from hinoki.modules import Module, order_modules
from hinoki.native import Router
from hinoki.openapi import OpenAPIBuilder
from hinoki.routing import Found, MethodNotAllowed, Route, route_of
from hinoki.rules import Phase, Project, RuleSet, failing
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.runtime.binding import Binding, bind
from hinoki.runtime.config import RuntimeConfig

log = logging.getLogger("hinoki")


class HinokiAPI:
    """A Hinoki application.

    Registering routes builds the native router; nothing connects to
    anything, so factories returning ``HinokiAPI`` are safe to import and call.
    The instance is itself an ASGI callable (``granian --interface asgi
    module:app``); the first call freezes the declarations.
    """

    def __init__(
        self,
        *,
        settings: object | None = None,
        runtime: RuntimeConfig | None = None,
        title: str = "Hinoki API",
        version: str = "0.1.0",
        openapi_url: str | None = None,
        docs_url: str | None = None,
        redoc_url: str | None = None,
        rules: RuleSet | None = None,
        enforce_rules: bool = True,
    ) -> None:
        self.settings = settings
        self.runtime = runtime or RuntimeConfig()
        self.title = title
        self.version = version
        self.openapi_url = openapi_url
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        docs_pages(openapi_url, docs_url, redoc_url)  # fail early on a bad combination
        self.rules = rules
        self.enforce_rules = enforce_rules
        self.errors = ErrorRegistry()
        self._routes: list[Route] = []
        self._bindings: list[Binding] = []
        self._modules: list[Module] = []
        self._router = Router()
        self._adapter: ASGIAdapter | None = None

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    @property
    def bindings(self) -> tuple[Binding, ...]:
        return tuple(self._bindings)

    @property
    def modules(self) -> tuple[Module, ...]:
        return tuple(self._modules)

    def include(self, *items: object) -> None:
        """Register decorated functions and ``API`` subclasses.

        All-or-nothing per call: on conflict the application is unchanged.
        """
        self._check_mutable()
        pending: list[Route] = []
        for item in items:
            if isinstance(item, type) and issubclass(item, API):
                pending.extend(item.routes())
            elif (route := route_of(item)) is not None:
                if route.replaces:
                    raise RegistrationError(
                        f"{route.name}: replaces=... only works inside an API class"
                    )
                pending.append(route)
            else:
                raise RegistrationError(
                    f"{item!r} is neither an API class nor a decorated function"
                )
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
