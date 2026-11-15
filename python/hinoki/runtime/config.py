"""Execution and lifecycle limits (proposed defaults, not measured)."""

from __future__ import annotations

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

    def __post_init__(self) -> None:
        if self.sync_workers < 1 or self.sync_queue < 0:
            raise ValueError("sync_workers must be >= 1 and sync_queue >= 0")
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if not 1 <= self.max_query_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError("max_query_bytes must be 1 to 1048576")
        if not 1 <= self.max_header_bytes <= MAX_REQUEST_INPUT_BYTES:
            raise ValueError("max_header_bytes must be 1 to 1048576")
        if min(self.request_timeout, self.startup_timeout, self.shutdown_timeout) <= 0:
            raise ValueError("timeouts must be positive")
