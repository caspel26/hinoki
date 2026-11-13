"""Request-scoped context. Set per request by the runtime, copied to threads."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextvars import ContextVar
from typing import Any

request_id: ContextVar[str | None] = ContextVar("hinoki_request_id", default=None)

# The event loop a sync handler's thread may hand coroutines back to.
event_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "hinoki_event_loop", default=None
)

# Futures of coroutines a sync handler handed to the loop (see hinoki.blocking);
# the executor cancels them when the call is cancelled.
handed_off: ContextVar[list[Future[Any]] | None] = ContextVar(
    "hinoki_handed_off", default=None
)
