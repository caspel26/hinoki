"""HTTP method decorators.

Decorators only attach metadata; nothing is registered, opened or started
until the function or API class is passed to ``HinokiAPI.include``. They
return the original callable unchanged, so signatures stay visible to type
checkers and decorators compose in any order.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeVar

from hinoki.access import Access
from hinoki.pagination import PageNumberPagination

P = ParamSpec("P")
R = TypeVar("R")

ROUTE_ATTR = "__hinoki_route__"
MIN_STATUS = 200
MAX_STATUS = 599


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

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    @property
    def name(self) -> str:
        return self.handler.__qualname__


class RouteDeclaration:
    """Decorator produced by ``@get(...)`` and friends.

    Validates the declaration eagerly and attaches the route to the function
    it decorates, returning that function unchanged.
    """

    def __init__(
        self,
        method: str,
        path: str,
        status: int,
        access: Access | None,
        transaction: bool,
        pagination: PageNumberPagination | None,
    ) -> None:
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        if not MIN_STATUS <= status <= MAX_STATUS:
            raise ValueError(f"invalid HTTP status {status}")
        self.method = method
        self.path = path
        self.status = status
        self.access = access
        self.transaction = transaction
        self.pagination = pagination

    def __call__(self, fn: Callable[P, R]) -> Callable[P, R]:
        if hasattr(fn, ROUTE_ATTR):
            raise ValueError(f"{fn.__qualname__} already declares an HTTP route")
        route = Route(
            self.method,
            self.path,
            fn,
            self.status,
            self.access,
            self.transaction,
            pagination=self.pagination,
        )
        setattr(fn, ROUTE_ATTR, route)
        return fn


def get(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
) -> RouteDeclaration:
    return RouteDeclaration("GET", path, status, access, transaction, pagination)


def post(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
) -> RouteDeclaration:
    return RouteDeclaration("POST", path, status, access, transaction, pagination)


def put(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
) -> RouteDeclaration:
    return RouteDeclaration("PUT", path, status, access, transaction, pagination)


def patch(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
) -> RouteDeclaration:
    return RouteDeclaration("PATCH", path, status, access, transaction, pagination)


def delete(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
    pagination: PageNumberPagination | None = None,
) -> RouteDeclaration:
    return RouteDeclaration("DELETE", path, status, access, transaction, pagination)


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
    found = getattr(obj, ROUTE_ATTR, None)
    return found if isinstance(found, Route) else None
