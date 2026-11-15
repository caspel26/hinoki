"""The checked-in examples use the manifest-based project workflow."""

import json
import shutil
from pathlib import Path

import pytest

from hinoki.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_manifest_lists_each_example_app_without_importing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(EXAMPLES)

    assert main(["app", "list", "--format", "json"]) == 0

    apps = json.loads(capsys.readouterr().out)["apps"]
    assert [app["name"] for app in apps] == ["notes", "tracker", "blog"]
    assert [app["models"] for app in apps] == [
        "notes.models",
        "tracker.models",
        "blog.models",
    ]


def test_manifest_applies_each_app_migration_to_a_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(EXAMPLES)
    database_url = f"sqlite://{tmp_path / 'examples.sqlite3'}"
    monkeypatch.setenv("HINOKI_DATABASE_URL", database_url)

    assert main(["migrations", "apply"]) == 0

    output = capsys.readouterr().out
    assert "notes: 0001-create-table-notes" in output
    assert "tracker: 0001-initial, 0002-indexes" in output
    assert "blog: 0001-initial, 0002-accounts" in output
    assert (tmp_path / "examples.sqlite3").is_file()


def test_app_create_adds_a_module_and_migration_package_to_the_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copy2(EXAMPLES / "hinoki.toml", tmp_path / "hinoki.toml")
    shutil.copytree(EXAMPLES / "project", tmp_path / "project")
    monkeypatch.chdir(tmp_path)

    assert main(["app", "create", "audit"]) == 0

    assert "created app audit" in capsys.readouterr().out
    assert (tmp_path / "project/apps/audit/models.py").is_file()
    assert (tmp_path / "project/apps/audit/migrations/__init__.py").is_file()
    manifest = (tmp_path / "hinoki.toml").read_text(encoding="utf-8")
    assert 'models = "project.apps.audit.models"' in manifest
    assert 'migrations = "project.apps.audit.migrations"' in manifest
