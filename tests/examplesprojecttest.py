"""The checked-in examples use the manifest-based project workflow."""

import json
import shutil
from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.scaffold import create

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_manifest_lists_each_example_app_without_importing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(EXAMPLES)

    assert main(["app", "list", "--format", "json"]) == 0

    apps = json.loads(capsys.readouterr().out)["apps"]
    assert [app["name"] for app in apps] == ["notes", "tracker", "blog", "admin"]
    assert [app["models"] for app in apps] == [
        "notes.models",
        "tracker.models",
        "blog.models",
        "hinoki.admin.users.models",
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
    assert "admin: 0001-initial" in output
    assert (tmp_path / "examples.sqlite3").is_file()


@pytest.mark.parametrize("source", ["implicit", "environment", "argument"])
def test_project_sqlite_default_is_root_anchored_but_explicit_relative_urls_are_not(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    project_name = f"journal{source}"
    project = create(project_name, tmp_path)
    workdir = project / project_name
    monkeypatch.chdir(workdir)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)

    args = ["migrations", "apply"]
    if source == "environment":
        monkeypatch.setenv("HINOKI_DATABASE_URL", "sqlite://./selected.sqlite3")
    elif source == "argument":
        args.extend(["--database", "sqlite://./selected.sqlite3"])

    assert main(args) == 0

    if source == "implicit":
        assert (project / ".hinoki" / "local.sqlite3").is_file()
        assert not (workdir / ".hinoki" / "local.sqlite3").exists()
    else:
        assert (workdir / "selected.sqlite3").is_file()
        assert not (project / ".hinoki" / "local.sqlite3").exists()


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
