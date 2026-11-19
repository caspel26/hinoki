from __future__ import annotations

import json
from pathlib import Path

from hinoki.runtime.recent import RequestRecorder, read_recent


def record(path: str, status: int = 200) -> str:
    return json.dumps({"path": path, "status": status})


def test_the_recorder_appends_and_rotates_one_generation(tmp_path: Path) -> None:
    file = tmp_path / "state" / "requests.jsonl"
    RequestRecorder(str(file))([record("/a"), record("/b")])
    assert [item["path"] for item in read_recent(file, 10)] == ["/a", "/b"]
    small = RequestRecorder(str(file), max_bytes=40)
    small([record("/c")])  # past the limit: the file becomes the previous generation
    small([record("/d")])
    small([record("/e")])
    paths = [item["path"] for item in read_recent(file, 10)]
    assert paths == ["/d", "/e"]


def test_reading_takes_the_newest_and_skips_broken_lines(tmp_path: Path) -> None:
    file = tmp_path / "requests.jsonl"
    file.write_text(f"{record('/a')}\nnot json\n[1]\n{record('/b')}\n")
    assert [item["path"] for item in read_recent(file, 1)] == ["/b"]
    assert read_recent(file, 0) == []
    assert read_recent(tmp_path / "missing.jsonl", 5) == []
