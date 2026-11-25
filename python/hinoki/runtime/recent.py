"""Recent requests for development tools: Kiso's JSON access records, appended
to a file that the MCP server reads. For ``hinoki dev``; workers append to the
same file, so it is a debugging aid and not an audit log."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hinoki import core

DEFAULT_PATH = ".hinoki/requests.jsonl"
# One generation is kept next to the live file, so history is at most twice this.
MAX_BYTES = 1_048_576


class RequestRecorder:
    """Appends each batch of JSON records to ``path`` and rotates it by size."""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def __call__(self, batch: Sequence[str]) -> None:
        core.append_recent(str(self.path), list(batch), self.max_bytes)


def read_recent(path: Path, limit: int) -> list[dict[str, Any]]:
    """The newest ``limit`` records, oldest first; unreadable lines are skipped."""
    return core.read_recent(str(path), limit)
