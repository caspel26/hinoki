"""Grouping of related actions under a common path prefix."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import ClassVar

from hinoki.errors import RegistrationError
from hinoki.routing import ROUTE_ATTR, Route, route_of
from hinoki.websocket import WS_ATTR, WebSocketRoute, websocket_of


class API:
    """Base class for a resource: several methods, standard or custom.

    ``class Posts(API, path='/posts')``. Instances hold no per-request state;
    methods declared with ``@http.get``/``@http.post``/... become actions.
    """

    prefix: ClassVar[str] = ""

    def __init_subclass__(cls, path: str | None = None, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if path is None:
            return  # inherit the parent's prefix
        if path and (not path.startswith("/") or path.endswith("/")):
            raise ValueError(
                f"{cls.__name__}: path must start with '/' and not end with '/': "
                f"{path!r}"
            )
        cls.prefix = path

    @classmethod
    def routes(cls) -> list[Route]:
        """Routes of this class with the prefix applied, in definition order.

        Members are resolved by name, most derived class first, so an override
        without a decorator removes the inherited route.
        """
        found: list[Route] = []
        for name in cls.member_names():
            route = cls.declared_route(name, inspect.getattr_static(cls, name))
            if route is not None:
                found.append(cls.mount(route))
        return found

    @classmethod
    def websockets(cls) -> list[WebSocketRoute]:
        """``@http.websocket`` methods with the prefix applied, in definition
        order; the class is created without arguments for each connection."""
        found: list[WebSocketRoute] = []
        for name in cls.member_names():
            member = inspect.getattr_static(cls, name)
            if isinstance(member, staticmethod | classmethod):
                continue  # declared_route already refused a decorated one
            declared = websocket_of(member)
            if declared is not None:
                path = cls.mounted_path(declared.path)
                found.append(replace(declared, path=path, owner=cls))
        return found

    @classmethod
    def member_names(cls) -> list[str]:
        """Member names, most derived class first, so an override without a
        decorator removes the inherited action."""
        return list(
            dict.fromkeys(n for klass in reversed(cls.__mro__) for n in vars(klass))
        )

    @classmethod
    def declared_route(cls, name: str, member: object) -> Route | None:
        if isinstance(member, staticmethod | classmethod):
            # the decorator sits above it, or below it
            declared = hasattr(member, ROUTE_ATTR) or hasattr(member, WS_ATTR)
            inner = member.__func__
            if declared or route_of(inner) is not None or websocket_of(inner):
                raise RegistrationError(
                    f"{cls.__name__}.{name}: route decorators go on plain methods, "
                    "not staticmethod/classmethod"
                )
            return None
        return route_of(member)

    @classmethod
    def mount(cls, route: Route) -> Route:
        return replace(route, path=cls.mounted_path(route.path), owner=cls)

    @classmethod
    def mounted_path(cls, path: str) -> str:
        is_root = path == "/" and cls.prefix
        return cls.prefix + ("" if is_root else path)
