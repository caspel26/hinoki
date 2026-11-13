"""Application object. Declarative only: it opens no resources."""

from __future__ import annotations

from hinoki._core import Router
from hinoki.api import API
from hinoki.routing import Route, route_of


class RegistrationError(Exception):
    """Invalid or conflicting declarations; raised at ``include`` time."""


class HinokiAPI:
    """A Hinoki application.

    Registering routes builds the native router; nothing connects to
    anything, so factories returning ``HinokiAPI`` are safe to import and call.
    """

    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._router = Router()

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    def include(self, *items: object) -> None:
        """Register decorated functions and ``API`` subclasses.

        All-or-nothing per call: on conflict the application is unchanged.
        """
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

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        """Find the action for a request line; ``None`` if unknown.

        A path that exists only for other methods also returns ``None`` here;
        the HTTP layer will distinguish 404 from 405 using the router result.
        """
        found = self._router.find(method, path)
        if found is None or isinstance(found[0], str):
            return None
        action, params = found
        assert isinstance(action, int)
        assert isinstance(params, dict)
        return self._routes[action], params
