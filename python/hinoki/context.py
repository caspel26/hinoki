"""Request-scoped context. Set per request by the runtime, copied to threads."""

from __future__ import annotations

from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("hinoki_request_id", default=None)
