"""The rate-limit seam: a route asks the application whether a request may proceed.

Hinoki ships no limiter. The application owns the quota, the key, the storage and
its sharing between workers; a route only names the policy that decides:

    @http.post("/auth/login", rate_limit=LoginLimit())
    async def login(data: LoginIn) -> SessionOut: ...

The policy runs after the route is matched and the caller authenticated, before the
body is read or the handler runs. It returns to allow the request and raises
``errors.TooManyRequests(retry_after)`` to refuse it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from hinoki.auth import Caller

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RateLimitContext:
    """What a policy may decide from: no headers, credentials, body or query."""

    method: str
    route: str  # the path template, for example ``/tasks/{task_id}``
    client: str | None  # the trusted client address, ``None`` when unknown
    caller: Caller[Any] | None  # the authenticated caller, ``None`` for a public route
    lookup: Callable[[type[Any]], Any] = field(repr=False)

    def resource(self, kind: type[T]) -> T:
        """The worker-owned resource of this type that a module provided (for
        example a Redis client or the ``Database``); ``LookupError`` if none."""
        found: T = self.lookup(kind)
        return found


class RateLimitPolicy(Protocol):
    """Anything with a ``check`` method, ``def`` or ``async def``.

    A sync ``check`` runs on the bounded executor and must use sync clients with
    finite timeouts; an async one runs on the event loop."""

    def check(self, context: RateLimitContext) -> Awaitable[None] | None: ...
