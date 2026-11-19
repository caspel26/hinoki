"""Recent requests for development tools: Kiso's JSON access records, appended
to a file that the MCP server reads. For ``hinoki dev``; workers append to the
same file, so it is a debugging aid and not an audit log."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_PATH = ".hinoki/requests.jsonl"
# One generation is kept next to the live file, so history is at most twice this.
MAX_BYTES = 1_048_576


class RequestRecorder:
    """Appends each batch of JSON records to ``path`` and rotates it by size."""

    def __init__(self, path: str, max_bytes: int = MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def __call__(self, batch: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(batch) + "\n")
        if self.path.stat().st_size > self.max_bytes:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))


def read_recent(path: Path, limit: int) -> list[dict[str, Any]]:
    """The newest ``limit`` records, oldest first; unreadable lines are skipped."""
    records: list[dict[str, Any]] = []
    for file in (path.with_name(path.name + ".1"), path):
        if not file.is_file():
            continue
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict):
                records.append(found)
    return records[-limit:] if limit > 0 else []
