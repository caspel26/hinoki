"""HTTP method decorators.

Decorators only attach metadata; nothing is registered, opened or started
until the function or API class is passed to ``Hinoki.include``. They
return the original callable unchanged, so signatures stay visible to type
checkers. For standalone actions, ``Hinoki.include`` uses the final decorated
callable. In a ``RouteGroup``, put the HTTP decorator outermost so the group
captures the final callable.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ParamSpec, TypeVar

from hinoki.access import Access
from hinoki.fields import UNSET, Unset
from hinoki.httperrors import APIError
from hinoki.pagination import PageNumberPagination
from hinoki.parsers import DEFAULT_PARSERS, Parser, check_parsers
from hinoki.renderers import DEFAULT_RENDERERS, Renderer, check_renderers

P = ParamSpec("P")
R = TypeVar("R")

ROUTE_ATTR = "__hinoki_route__"
MIN_STATUS = 200
MAX_STATUS = 599
MIN_ERROR_STATUS = 400
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Route:
    """Declared contract of one action, before it is bound to an application."""

    method: str
    path: str
    handler: Callable[..., object]
    status: int
    access: Access | None
    transaction: bool
    owner: type | None = None
    pagination: PageNumberPagination | None = None
    replaces: str = ""
    parsers: tuple[Parser, ...] = DEFAULT_PARSERS
    renderers: tuple[Renderer, ...] = DEFAULT_RENDERERS
    errors: tuple[type[APIError], ...] = ()

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    @property
    def name(self) -> str:
        return self.handler.__qualname__


@dataclass(frozen=True, slots=True)
class Options:
    """Keyword options shared by every HTTP method decorator."""

    status: int = 200
    access: Access | None = None
    transaction: bool = False
    pagination: PageNumberPagination | None = None
    replaces: str = ""
    parsers: Sequence[Parser] | None = None
    renderers: Sequence[Renderer] | None = None
    errors: Sequence[type[APIError]] = ()


class RouteDeclaration:
    """Decorator produced by ``@http.get(...)`` and friends.

    Validates the declaration eagerly and attaches the route to the function
    it decorates, returning that function unchanged.
    """

    def __init__(self, method: str, path: str, options: Options) -> None:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        if not MIN_STATUS <= options.status <= MAX_STATUS:
            raise ValueError(f"invalid HTTP status {options.status}")
        self.method = method
        self.path = path
        self.options = options

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        if hasattr(fn, ROUTE_ATTR):
            raise ValueError(f"{fn.__qualname__} already declares an HTTP route")
        options = self.options
        route = Route(
            self.method,
            self.path,
            fn,
            options.status,
            options.access,
            options.transaction,
            pagination=options.pagination,
            replaces=options.replaces,
            parsers=(
                DEFAULT_PARSERS
                if options.parsers is None
                else check_parsers(options.parsers)
            ),
            renderers=(
                DEFAULT_RENDERERS
                if options.renderers is None
                else check_renderers(options.renderers)
            ),
            errors=check_errors(options.errors),
        )
        setattr(fn, ROUTE_ATTR, route)
        return fn


def check_errors(errors: Sequence[type[APIError]]) -> tuple[type[APIError], ...]:
    """The declared errors, each an ``APIError`` subclass with a 4xx/5xx status."""
    found: list[type[APIError]] = []
    for error in errors:
        if not (isinstance(error, type) and issubclass(error, APIError)):
            raise ValueError(f"errors=... takes APIError subclasses, got {error!r}")
        if not MIN_ERROR_STATUS <= int(error.status) <= MAX_STATUS:
            raise ValueError(f"{error.__name__}.status must be 400 to 599")
        if error not in found:
            found.append(error)
    return tuple(found)


def get(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
    replaces: str = "",
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
) -> RouteDeclaration:
    """Declare a GET action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    options = Options(
        status, access, transaction, pagination, replaces, parsers, renderers, errors
    )
    return RouteDeclaration("GET", path, options)


def post(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
    replaces: str = "",
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
) -> RouteDeclaration:
    """Declare a POST action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    options = Options(
        status, access, transaction, pagination, replaces, parsers, renderers, errors
    )
    return RouteDeclaration("POST", path, options)


def put(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
    replaces: str = "",
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
) -> RouteDeclaration:
    """Declare a PUT action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    options = Options(
        status, access, transaction, pagination, replaces, parsers, renderers, errors
    )
    return RouteDeclaration("PUT", path, options)


def patch(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
    replaces: str = "",
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
) -> RouteDeclaration:
    """Declare a PATCH action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    options = Options(
        status, access, transaction, pagination, replaces, parsers, renderers, errors
    )
    return RouteDeclaration("PATCH", path, options)


def delete(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
    replaces: str = "",
    parsers: Sequence[Parser] | None = None,
    renderers: Sequence[Renderer] | None = None,
    errors: Sequence[type[APIError]] = (),
) -> RouteDeclaration:
    """Declare a DELETE action. ``errors`` lists the ``APIError`` subclasses the
    action may raise; each carries its own status and code, which OpenAPI and the
    MCP tool description then show."""
    options = Options(
        status, access, transaction, pagination, replaces, parsers, renderers, errors
    )
    return RouteDeclaration("DELETE", path, options)


class RouteGroup:
    """A function-based route group with a shared path prefix and defaults.

    Decorated functions belong to this group and the group can be included once
    in a ``Hinoki`` application. Use ``API`` for class-based resources.

        tasks = RouteGroup("/tasks", access=member_only, atomic=True,
                           errors=(NotFound,))

        @tasks.get("/")                      # GET /tasks
        async def listing(...): ...

        @tasks.post("/", status=201)         # POST /tasks, transaction=True
        async def create(...): ...

        @tasks.get("/health", access=None)   # public on purpose, overriding the group

    ``prefix`` is prepended to every path (``"/"`` means the prefix itself),
    ``access`` is the default policy (pass ``access=None`` on an action to make it
    public), ``atomic=True`` makes POST/PUT/PATCH/DELETE transactional unless the
    action says ``transaction=False``, and ``errors`` are added to each action's own.
    The result is an ordinary action: rules, OpenAPI and the MCP see no difference.
    """

    def __init__(
        self,
        prefix: str = "",
        *,
        access: Access | None = None,
        atomic: bool = False,
        errors: Sequence[type[APIError]] = (),
    ) -> None:
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            raise ValueError("a prefix starts with '/' and does not end with one")
        self.prefix = prefix
        self.access = access
        self.atomic = atomic
        self.errors = tuple(errors)
        self._handlers: list[Callable[..., object]] = []
        self._included = False

    @property
    def handlers(self) -> tuple[Callable[..., object], ...]:
        """The decorated handlers in declaration order."""
        return tuple(self._handlers)

    def _include(self) -> None:
        self._included = True

    def _decorate(
        self, declaration: RouteDeclaration, fn: Callable[P, R]
    ) -> Callable[P, R]:
        if self._included:
            raise RuntimeError("a RouteGroup cannot change after it is included")
        decorated = declaration(fn)
        self._handlers.append(decorated)
        return decorated

    def declare(
        self, method: str, path: str, options: dict[str, Any]
    ) -> RouteDeclaration:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        full = (self.prefix or "/") if path == "/" else self.prefix + path
        access = options.pop("access", UNSET)
        transaction = options.pop("transaction", None)
        own_errors = tuple(options.pop("errors", ()))
        chosen = Options(
            status=options.pop("status", 200),
            access=self.access if isinstance(access, Unset) else access,
            transaction=(
                transaction
                if transaction is not None
                else self.atomic and method in WRITE_METHODS
            ),
            pagination=options.pop("pagination", None),
            replaces=options.pop("replaces", ""),
            parsers=options.pop("parsers", None),
            renderers=options.pop("renderers", None),
            errors=(*self.errors, *own_errors),
        )
        if options:
            raise TypeError(f"unknown action options {sorted(options)}")
        return RouteDeclaration(method, full, chosen)

    def get(self, path: str, **options: Any) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("GET", path, options))

    def post(self, path: str, **options: Any) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("POST", path, options))

    def put(self, path: str, **options: Any) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("PUT", path, options))

    def patch(self, path: str, **options: Any) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("PATCH", path, options))

    def delete(self, path: str, **options: Any) -> _GroupRouteDeclaration:
        return _GroupRouteDeclaration(self, self.declare("DELETE", path, options))


class _GroupRouteDeclaration:
    """Attach a group's route metadata and retain the handler for inclusion."""

    def __init__(self, group: RouteGroup, declaration: RouteDeclaration) -> None:
        self.group = group
        self.declaration = declaration

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        return self.group._decorate(self.declaration, fn)


@dataclass(frozen=True, slots=True)
class Found:
    """The router matched an action."""

    action: int
    params: dict[str, str]


@dataclass(frozen=True, slots=True)
class MethodNotAllowed:
    """The path exists, but not for this method."""

    allowed: list[str]


def route_of(obj: object) -> Route | None:
    if not callable(obj):
        return None
    found = getattr(obj, ROUTE_ATTR, None)
    if not isinstance(found, Route):
        return None
    # functools.wraps copies route metadata, but the stored declaration still
    # points at the original function. Dispatch through the final wrapper.
    return found if found.handler is obj else replace(found, handler=obj)
