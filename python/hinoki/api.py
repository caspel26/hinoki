"""Grouping of related actions under a common path prefix."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import ClassVar

from hinoki.errors import RegistrationError
from hinoki.routing import Route, route_of


class API:
    """Base class for a resource: several methods, standard or custom.

    ``class Posts(API, path='/posts')``. Instances hold no per-request state;
    methods declared with ``@get``/``@post``/... become actions.
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
        names = dict.fromkeys(n for klass in reversed(cls.__mro__) for n in vars(klass))
        found: list[Route] = []
        for name in names:
            route = cls.declared_route(name, inspect.getattr_static(cls, name))
            if route is not None:
                found.append(cls.mount(route))
        return found

    @classmethod
    def declared_route(cls, name: str, member: object) -> Route | None:
        if isinstance(member, staticmethod | classmethod):
            if route_of(member.__func__) is not None:
                raise RegistrationError(
                    f"{cls.__name__}.{name}: route decorators go on plain methods, "
                    "not staticmethod/classmethod"
                )
            return None
        return route_of(member)

    @classmethod
    def mount(cls, route: Route) -> Route:
        is_root = route.path == "/" and cls.prefix
        path = cls.prefix + ("" if is_root else route.path)
        return replace(route, path=path, owner=cls)
