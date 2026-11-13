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

P = ParamSpec("P")
R = TypeVar("R")

ROUTE_ATTR = "__hinoki_route__"


@dataclass(frozen=True, slots=True)
class Route:
    """Declared contract of one action, before it is bound to an application."""

    method: str
    path: str
    handler: Callable[..., object]
    status: int
    access: Access | None
    transaction: bool

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    @property
    def name(self) -> str:
        return self.handler.__qualname__


def _decorator(
    method: str, path: str, status: int, access: Access | None, transaction: bool
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    if not path.startswith("/"):
        raise ValueError(f"route path must start with '/': {path!r}")
    if not 100 <= status <= 599:
        raise ValueError(f"invalid HTTP status {status}")

    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        if hasattr(fn, ROUTE_ATTR):
            raise ValueError(f"{fn.__qualname__} already declares an HTTP route")
        setattr(fn, ROUTE_ATTR, Route(method, path, fn, status, access, transaction))
        return fn

    return wrap


def get(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    return _decorator("GET", path, status, access, transaction)


def post(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    return _decorator("POST", path, status, access, transaction)


def put(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    return _decorator("PUT", path, status, access, transaction)


def patch(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    return _decorator("PATCH", path, status, access, transaction)


def delete(
    path: str,
    *,
    status: int = 200,
    access: Access | None = None,
    transaction: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    return _decorator("DELETE", path, status, access, transaction)


def route_of(obj: object) -> Route | None:
    found = getattr(obj, ROUTE_ATTR, None)
    return found if isinstance(found, Route) else None
