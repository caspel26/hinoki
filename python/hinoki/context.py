"""Request-scoped context. Set per request by the runtime, copied to threads."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("hinoki_request_id", default=None)

# The event loop a sync handler's thread may hand coroutines back to.
event_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "hinoki_event_loop", default=None
)
