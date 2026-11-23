"""Migration command helper behavior at filesystem and dependency boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hinoki import db
from hinoki.cli import migrations as projectmigrations
from hinoki.db import Database
from hinoki.errors import MigrationError
from hinoki.orm import Model, field, pk
from hinoki.project import ProjectApp, ProjectError, ProjectManifest
from hinoki.settings import DatabaseSettings


def _manifest(root: Path, apps: tuple[ProjectApp, ...] = ()) -> ProjectManifest:
    path = root / "hinoki.toml"
    path.touch()
    return ProjectManifest(path, "sample.main:app", apps)


def _settings_with_url(url: str) -> type:
    class Settings:
        def __init__(self) -> None:
            self.url = SimpleNamespace(reveal=self.reveal)

        def reveal(self) -> str:
            return url

    return Settings


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            ModuleNotFoundError("No module named 'gone'", name="gone"),
            r"cannot import the application sample\.main:app "
            r"\(ModuleNotFoundError: 'gone'\)",
        ),
        (
            RuntimeError("boom"),
            r"cannot load the application sample\.main:app \(RuntimeError\)",
        ),
        (
            SystemExit(3),
            r"cannot load the application sample\.main:app \(SystemExit\)",
        ),
    ],
)
def test_an_application_that_cannot_load_stops_the_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    message: str,
) -> None:
    def fail(_target: str) -> object:
        raise failure

    monkeypatch.delenv(projectmigrations.URL_VARIABLE, raising=False)
    monkeypatch.setattr(projectmigrations, "build_app", fail)
    args = argparse.Namespace(database=None)

    with pytest.raises(projectmigrations.DatabaseResolutionError, match=message):
        projectmigrations.migration_database(args, _manifest(tmp_path))


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("postgres://u:pw@h:5432/db", "postgres://***@h:5432/db"),
        ("postgres://u:p/w@h/db", "postgres://***@h/db"),
        ("postgres://u:p?w@h/db?sslmode=require", "postgres://***@h/db?..."),
        ("postgres://u:p#w@h/db", "postgres://***@h/db"),
        ("postgres://u:p@ss@h/db", "postgres://***@h/db"),
        ("postgres://h/db", "postgres://h/db"),
        ("sqlite:///srv/app.sqlite3", "sqlite:///srv/app.sqlite3"),
        ("u:pw@h/db", "<unparseable URL>"),
        ("postgres://u:pw@h/db password=pw", "postgres://<unparseable URL>"),
        ("mysql://u:pw@[::1/db", "mysql://<unparseable URL>"),
    ],
)
def test_redacted_url_never_shows_credentials(url: str, shown: str) -> None:
    assert projectmigrations.redacted_url(url) == shown


def test_migration_database_url_uses_explicit_environment_and_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        projectmigrations, "build_app", lambda _target: SimpleNamespace(settings=None)
    )
    args = argparse.Namespace(database="postgres://explicit/db")
    assert projectmigrations.migration_database_url(args, manifest) == args.database

    args.database = None
    monkeypatch.setenv(projectmigrations.URL_VARIABLE, "mysql://env/db")
    assert projectmigrations.migration_database_url(args, manifest) == "mysql://env/db"
    monkeypatch.delenv(projectmigrations.URL_VARIABLE)

    monkeypatch.setattr(
        projectmigrations,
        "DatabaseSettings",
        _settings_with_url("sqlite://.hinoki/local.sqlite3"),
    )
    result = projectmigrations.migration_database_url(args, manifest)
    assert result == f"sqlite://{(tmp_path / '.hinoki/local.sqlite3').resolve()}"
    absolute = tmp_path / "absolute.sqlite3"
    monkeypatch.setattr(
        projectmigrations,
        "DatabaseSettings",
        _settings_with_url(f"sqlite://{absolute}"),
    )
    assert (
        projectmigrations.migration_database_url(args, manifest)
        == f"sqlite://{absolute}"
    )
    monkeypatch.setattr(
        projectmigrations,
        "DatabaseSettings",
        _settings_with_url("postgres://default/db"),
    )
    assert (
        projectmigrations.migration_database_url(args, manifest)
        == "postgres://default/db"
    )


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        ("sqlite://local.db", "sqlite"),
        ("mysql://db", "mysql"),
        ("mariadb://db", "mariadb"),
        ("postgres://db", "postgres"),
        ("postgresql://db", "postgres"),
    ],
)
def test_database_dialect_accepts_supported_schemes(url: str, dialect: str) -> None:
    assert projectmigrations.database_dialect(url) == dialect


def test_database_dialect_rejects_unknown_scheme() -> None:
    with pytest.raises(MigrationError, match="supported backend scheme"):
        projectmigrations.database_dialect("oracle://db")


def test_manifest_for_rejects_project_only_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(projectmigrations, "find_manifest_or_none", lambda: None)
    with pytest.raises(ProjectError, match="--app requires"):
        projectmigrations.manifest_for(argparse.Namespace(app="billing", dir=None))
    assert (
        projectmigrations.manifest_for(argparse.Namespace(app=None, dir=None)) is None
    )

    path = tmp_path / "hinoki.toml"
    path.touch()
    monkeypatch.setattr(projectmigrations, "find_manifest_or_none", lambda: path)
    with pytest.raises(ProjectError, match="do not combine"):
        projectmigrations.manifest_for(
            argparse.Namespace(app=None, dir="migrations", models=None)
        )


def test_migration_history_reports_missing_invalid_and_valid_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = tmp_path / "0001-initial.sql"
    sql.touch()
    monkeypatch.setattr(projectmigrations, "migration_files", lambda _directory: [sql])

    with pytest.raises(MigrationError, match=r"cannot read 0001-initial\.state\.json"):
        projectmigrations._migration_history("accounts", tmp_path)

    state_path = sql.with_suffix(".state.json")
    state_path.write_text("{", encoding="utf-8")
    with pytest.raises(MigrationError, match="not valid JSON"):
        projectmigrations._migration_history("accounts", tmp_path)

    state_path.write_text(
        json.dumps(
            {
                "tables": {
                    "widgets": {
                        "columns": {
                            "id": {
                                "type": "integer",
                                "primary_key": True,
                                "references": None,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    history = projectmigrations._migration_history("accounts", tmp_path)
    assert history[0]["tables"]["widgets"]["columns"]["id"]["type"] == "bigint"


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"tables": []},
        {"tables": {"widgets": None}},
        {"tables": {"widgets": {"columns": []}}},
        {"tables": {"widgets": {"columns": {"id": None}}}},
        {"tables": {"widgets": {"columns": {"id": {"type": 1, "primary_key": True}}}}},
        {
            "tables": {
                "widgets": {
                    "columns": {
                        "id": {
                            "type": "integer",
                            "primary_key": True,
                            "references": "bad",
                        }
                    }
                }
            }
        },
    ],
)
def test_validate_migration_state_rejects_malformed_shapes(state: object) -> None:
    with pytest.raises(MigrationError):
        projectmigrations._validate_migration_state("billing", "state.json", state)


def test_project_ownership_and_cross_app_target_validation() -> None:
    assert projectmigrations._external_owner("billing", "unowned", {}) is None
    with pytest.raises(MigrationError, match="ambiguous migration ownership"):
        projectmigrations._external_owner(
            "billing", "accounts", {"accounts": {"one", "two"}}
        )

    candidate = {
        "tables": {
            "invoices": {
                "columns": {
                    "account_id": {
                        "type": "bigint",
                        "references": {"table": "accounts", "column": "id"},
                    }
                }
            }
        }
    }
    recorded = {
        "tables": {
            "accounts": {"columns": {"id": {"type": "bigint", "primary_key": True}}}
        }
    }
    owners = {"accounts": {"core"}}
    projectmigrations._validate_recorded_prerequisites(
        "billing", candidate, owners, {"core": recorded}
    )
    unowned = {
        "tables": {
            "invoices": {
                "columns": {
                    "other_id": {
                        "type": "bigint",
                        "references": {"table": "external", "column": "id"},
                    }
                }
            }
        }
    }
    projectmigrations._validate_recorded_prerequisites("billing", unowned, owners, {})

    missing: dict[str, object] = {"tables": {}}
    with pytest.raises(MigrationError, match="absent from recorded migrations"):
        projectmigrations._check_recorded_target(
            "billing",
            "core",
            "invoices",
            "account_id",
            candidate["tables"]["invoices"]["columns"]["account_id"],
            {"table": "accounts", "column": "missing"},
            recorded,
        )
    with pytest.raises(MigrationError, match="incompatible with recorded target"):
        projectmigrations._check_recorded_target(
            "billing",
            "core",
            "invoices",
            "account_id",
            candidate["tables"]["invoices"]["columns"]["account_id"],
            {"table": "accounts", "column": "id"},
            {
                "tables": {
                    "accounts": {
                        "columns": {"id": {"type": "text", "primary_key": False}}
                    }
                }
            },
        )
    assert missing == {"tables": {}}


def test_cross_app_teardown_allows_no_old_target_but_rejects_changed_target() -> None:
    old = {"type": "bigint", "primary_key": True}
    owners = {"accounts": {"core"}}
    history = {
        "billing": [
            {
                "tables": {
                    "invoices": {
                        "columns": {
                            "account_id": {
                                "references": {"table": "accounts", "column": "id"}
                            }
                        }
                    }
                }
            }
        ]
    }
    projectmigrations._reject_cross_app_teardown(
        "core", {"tables": {}}, {"tables": {}}, owners, history
    )
    recorded = {"tables": {"accounts": {"columns": {"id": old}}}}
    with pytest.raises(MigrationError, match="separate reviewed deployment phase"):
        projectmigrations._reject_cross_app_teardown(
            "core", recorded, {"tables": {"accounts": {"columns": {}}}}, owners, history
        )
    assert projectmigrations._recorded_column(recorded, "unknown", "id") is None
    assert projectmigrations._target_was_removed_or_changed(old, None)


@pytest.mark.asyncio
async def test_run_database_project_applies_and_lists_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = ProjectApp("sample", "sample.models", "sample.migrations", ())
    manifest = _manifest(tmp_path, (app,))
    directory = tmp_path / "migrations"
    directories = {"sample": directory}
    calls: list[tuple[str, Path, str]] = []

    async def apply(_database: Database, path: Path, *, namespace: str) -> list[str]:
        calls.append(("apply", path, namespace))
        return ["0001-initial"]

    async def status(
        _database: Database, path: Path, *, namespace: str
    ) -> list[tuple[str, str]]:
        calls.append(("status", path, namespace))
        return [("0001-initial", "applied")]

    monkeypatch.setattr("hinoki.cli.migrations.migrator.apply", apply)
    monkeypatch.setattr("hinoki.cli.migrations.migrator.status", status)
    await projectmigrations.run_database_project(
        argparse.Namespace(app=None, action="apply"),
        manifest,
        cast(Database, object()),
        directories,
    )
    assert "sample: 0001-initial" in capsys.readouterr().out
    await projectmigrations.run_database_project(
        argparse.Namespace(app=None, action="status"),
        manifest,
        cast(Database, object()),
        directories,
    )
    assert "sample           applied  0001-initial" in capsys.readouterr().out
    assert [call[0] for call in calls] == ["apply", "status"]


def test_sql_project_prints_matching_migration_or_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = ProjectApp("sample", "sample.models", "sample.migrations", ())
    manifest = _manifest(tmp_path, (app,))
    directory = tmp_path / "migrations"
    migration = directory / "0001-initial.sql"
    directory.mkdir()
    migration.write_text("CREATE TABLE widgets (id BIGINT);\n", encoding="utf-8")
    monkeypatch.setattr(
        ProjectManifest,
        "migration_directories",
        lambda _manifest: {"sample": directory},
    )
    args = argparse.Namespace(app="sample", name="0001-initial")
    projectmigrations.sql_project(args, manifest)
    assert capsys.readouterr().out == migration.read_text(encoding="utf-8")

    args.name = "9999-missing"
    with pytest.raises(MigrationError, match="has no migration named"):
        projectmigrations.sql_project(args, manifest)


def test_plan_project_sees_constraint_names_of_unrelated_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def paired(table: str) -> type[Model]:
        @db.unique("a", "b", name="uq_shared_pair")
        class Pair(Model, table=table):
            id: int = pk()
            a: str = field(max_length=20, default="")
            b: str = field(max_length=20, default="")

        return Pair

    apps = (
        ProjectApp("billing", "sample.billing", "billing_migrations", ()),
        ProjectApp("crm", "sample.crm", "crm_migrations", ()),
    )
    manifest = _manifest(tmp_path, apps)
    models = {"billing": [paired("pp_invoices")], "crm": [paired("pp_contacts")]}
    monkeypatch.setattr(ProjectManifest, "all_models", lambda _self: models)
    args = argparse.Namespace(app="billing")
    with pytest.raises(MigrationError, match=r"uq_shared_pair.*pp_invoices"):
        projectmigrations.plan_project(args, manifest)


def test_the_default_local_database_is_not_a_declared_application_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    app = SimpleNamespace(settings=SimpleNamespace(database=DatabaseSettings()))
    monkeypatch.setattr(projectmigrations, "build_app", lambda _target: app)
    monkeypatch.delenv(projectmigrations.URL_VARIABLE, raising=False)
    database = projectmigrations.migration_database(
        argparse.Namespace(database=None), manifest
    )
    assert database.source == projectmigrations.DEFAULT_SOURCE


def test_plan_project_takes_the_dialect_from_the_resolved_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Note(Model, table="pp_dialect_notes"):
        id: int = pk()
        text: str = field(max_length=20, default="")

    apps = (ProjectApp("notes", "sample.notes", "notes_migrations", ()),)
    manifest = _manifest(tmp_path, apps)
    directory = tmp_path / "migrations"
    directory.mkdir()
    monkeypatch.setattr(ProjectManifest, "all_models", lambda _self: {"notes": [Note]})
    monkeypatch.setattr(
        ProjectManifest, "migration_directories", lambda _self: {"notes": directory}
    )
    args = argparse.Namespace(
        app="notes",
        name=None,
        database="sqlite:///" + str(tmp_path / "d.db"),
        allow_destructive=False,
        check=False,
        dry_run=True,
    )
    result = projectmigrations.plan_project(args, manifest)
    assert len(result.operations) == 1
