"""Middleware: code that runs around an action, the same on every server.

    from hinoki import Hinoki, http
    from hinoki.http import Middleware, MiddlewareRequest, Next, Response

    class Tenant(Middleware):
        async def __call__(
            self, request: MiddlewareRequest, call_next: Next
        ) -> Response:
            if "x-tenant" not in request.headers:
                return Response(400, "missing x-tenant", content_type="text/plain")
            return (await call_next()).with_header("x-served-by", "tenant")

    @http.get("/items", middleware=[Tenant()])
    async def items() -> list[Item]: ...

    app = Hinoki(middleware=[Audit()])   # runs before the action's own

A middleware runs after the server's own answers (documentation, CORS
preflight, redirects, an unknown route) and after the route is chosen, before
authentication, so it sees the head of the request and never its body. The
``Response`` that ``call_next`` returns is final for the action: an error
the action raised is already an HTTP answer and its transaction is already
rolled back. Security, CORS and request-id headers are added after the
middleware returns.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from hinoki.http.response import Response

__all__ = ["Middleware", "MiddlewareRequest", "Next"]


@dataclass(frozen=True, slots=True)
class MiddlewareRequest:
    """The head of the request: no body, nothing to change."""

    method: str
    path: str
    route: str  # the matched route template, e.g. ``/items/{item_id}``
    headers: Mapping[str, str]  # lower-case names; repeated headers joined by ", "
    query: str
    client: tuple[str, int] | None
    scheme: str
    request_id: str


Next = Callable[[], Awaitable[Response]]


class Middleware:
    """Base class for a middleware; any object with this ``async`` call works."""

    async def __call__(
        self,
        request: MiddlewareRequest,  # noqa: ARG002 - part of the contract
        call_next: Next,
    ) -> Response:
        return await call_next()


def check_middleware(items: Sequence[object]) -> tuple[Middleware, ...]:
    """The middleware as a tuple, each an object with an ``async`` call."""
    found: list[Middleware] = []
    for item in items:
        call = getattr(item, "__call__", None)  # noqa: B004 - the contract itself
        if call is None or not inspect.iscoroutinefunction(call):
            raise TypeError(
                f"middleware=... takes objects with an `async def __call__"
                f"(request, call_next)`, got {item!r}"
            )
        found.append(item)  # type: ignore[arg-type]
    return tuple(found)
