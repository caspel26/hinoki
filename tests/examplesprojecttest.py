"""The checked-in examples use the manifest-based project workflow."""

import json
from pathlib import Path

import pytest

from hinoki.__main__ import main

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_lists_each_example_app_without_importing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)

    assert main(["app", "list", "--format", "json"]) == 0

    apps = json.loads(capsys.readouterr().out)["apps"]
    assert [app["name"] for app in apps] == ["notes", "tracker", "blog"]
    assert [app["models"] for app in apps] == [
        "examples.notes.models",
        "examples.tracker.models",
        "examples.blog.models",
    ]


def test_manifest_applies_each_app_migration_to_a_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(ROOT)
    database_url = f"sqlite://{tmp_path / 'examples.sqlite3'}"
    monkeypatch.setenv("HINOKI_DATABASE_URL", database_url)

    assert main(["migrations", "apply"]) == 0

    output = capsys.readouterr().out
    assert "notes: 0001-create-table-notes" in output
    assert "tracker: 0001-initial, 0002-indexes" in output
    assert "blog: 0001-initial, 0002-accounts" in output
    assert (tmp_path / "examples.sqlite3").is_file()
