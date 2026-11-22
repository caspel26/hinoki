"""Execution and lifecycle limits (proposed defaults, not measured)."""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_REQUEST_INPUT_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    sync_workers: int = 8
    sync_queue: int = 64
    request_timeout: float = 30.0
    startup_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    max_body_bytes: int = 1024 * 1024
    max_query_bytes: int = 8192
    max_header_bytes: int = 32768
    max_streams: int = 64
    stream_idle_timeout: float | None = None  # None: `request_timeout`
    sse_keepalive: float = 15.0
    max_websockets: int = 1000
    ws_max_message_bytes: int = 1024 * 1024
    ws_queue: int = 64
    ws_ping_interval: float | None = 20.0
    ws_pong_timeout: float = 20.0
    ws_close_timeout: float = 5.0

    @property
    def idle_timeout(self) -> float:
        """Seconds a stream's producer may stay silent."""
        return self.stream_idle_timeout or self.request_timeout

    def __post_init__(self) -> None:
        if self.max_streams < 1:
            raise ValueError("max_streams must be at least 1")
        if self.sync_workers < 1 or self.sync_queue < 0:
            raise ValueError("sync_workers must be >= 1 and sync_queue >= 0")
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if not 1 <= self.max_query_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError("max_query_bytes must be 1 to 1048576")
        if not 1 <= self.max_header_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError("max_header_bytes must be 1 to 1048576")
        timeouts = (
            self.request_timeout,
            self.startup_timeout,
            self.shutdown_timeout,
            self.sse_keepalive,
            self.stream_idle_timeout or 1.0,
            self.ws_ping_interval or 1.0,
            self.ws_pong_timeout,
            self.ws_close_timeout,
        )
        if not all(math.isfinite(value) and value > 0 for value in timeouts):
            raise ValueError("timeouts must be positive and finite")
