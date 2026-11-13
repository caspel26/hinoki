"""Execution and lifecycle limits (proposed defaults, not measured)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    sync_workers: int = 8
    sync_queue: int = 64
    request_timeout: float = 30.0
    startup_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    max_body_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.sync_workers < 1 or self.sync_queue < 0:
            raise ValueError("sync_workers must be >= 1 and sync_queue >= 0")
        if self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if min(self.request_timeout, self.startup_timeout, self.shutdown_timeout) <= 0:
            raise ValueError("timeouts must be positive")
