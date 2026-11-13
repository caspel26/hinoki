"""Request-scoped context. Set per request by the runtime, copied to threads."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from contextvars import ContextVar
from typing import Any

request_id: ContextVar[str | None] = ContextVar("hinoki_request_id", default=None)

# The event loop a sync handler's thread may hand coroutines back to.
event_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "hinoki_event_loop", default=None
)


class Handoff:
    """Coroutines a sync handler handed to the loop (see hinoki.blocking).

    The executor closes it when the call is cancelled: pending work is cancelled
    and later hand-offs from the abandoned thread are refused, so it cannot keep
    using the pool after the request ended."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.futures: set[Future[Any]] = set()
        self.closed = False

    def add(self, future: Future[Any]) -> bool:
        with self.lock:
            if self.closed:
                return False
            self.futures.add(future)
            return True

    def discard(self, future: Future[Any]) -> None:
        with self.lock:
            self.futures.discard(future)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            pending = list(self.futures)
            self.futures.clear()
        for future in pending:
            future.cancel()


handed_off: ContextVar[Handoff | None] = ContextVar("hinoki_handed_off", default=None)


# Headers an API class asks to have on every answer under its routes, including
# the errors the framework builds (401, 403, 404, 500). Set once the route is known.
route_headers: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "hinoki_route_headers", default=()
)
