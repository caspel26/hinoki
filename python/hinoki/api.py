"""Grouping of related actions under a common path prefix."""

from __future__ import annotations

from typing import ClassVar

from hinoki.routing import Route, route_of


class API:
    """Base class for a resource: several methods, standard or custom.

    ``class Posts(API, path='/posts')``. Instances hold no per-request state;
    methods declared with ``@get``/``@post``/... become actions.
    """

    prefix: ClassVar[str] = ""

    def __init_subclass__(cls, path: str = "", **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if path and (not path.startswith("/") or path.endswith("/")):
            raise ValueError(
                f"{cls.__name__}: path must start with '/' and not end with '/': "
                f"{path!r}"
            )
        cls.prefix = path

    @classmethod
    def routes(cls) -> list[Route]:
        """Routes of this class with the prefix applied, in definition order."""
        found: list[Route] = []
        for klass in reversed(cls.__mro__):
            for value in vars(klass).values():
                route = route_of(value)
                if route is None:
                    continue
                path = cls.prefix + (
                    "" if route.path == "/" and cls.prefix else route.path
                )
                found.append(
                    Route(
                        route.method,
                        path,
                        route.handler,
                        route.status,
                        route.access,
                        route.transaction,
                        cls,
                    )
                )
        return found
