"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from hinoki.api import API
from hinoki.errors import RegistrationError
from hinoki.modules import Module, order_modules
from hinoki.native import Router
from hinoki.routing import Found, MethodNotAllowed, Route, route_of
from hinoki.runtime.asgi import ASGIAdapter
from hinoki.runtime.config import RuntimeConfig


class HinokiAPI:
    """A Hinoki application.

    Registering routes builds the native router; nothing connects to
    anything, so factories returning ``HinokiAPI`` are safe to import and call.
    The instance is itself an ASGI callable (``granian --interface asgi
    module:app``); the first call freezes the declarations.
    """

    def __init__(
        self, *, settings: object | None = None, runtime: RuntimeConfig | None = None
    ) -> None:
        self.settings = settings
        self.runtime = runtime or RuntimeConfig()
        self._routes: list[Route] = []
        self._modules: list[Module] = []
        self._router = Router()
        self._adapter: ASGIAdapter | None = None

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

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
                pending.append(route)
            else:
                raise RegistrationError(
                    f"{item!r} is neither an API class nor a decorated function"
                )
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

    def asgi(self) -> ASGIAdapter:
        """The ASGI adapter for this worker; builds and validates bindings once."""
        if self._adapter is None:
            self._adapter = ASGIAdapter(self)
        return self._adapter

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: Any, send: Any
    ) -> None:
        await self.asgi()(scope, receive, send)

    def _check_mutable(self) -> None:
        if self._adapter is not None:
            raise RegistrationError("application is already serving; it is frozen")
