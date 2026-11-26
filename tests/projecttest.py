"""Project registry and modular-app commands."""

import argparse
import importlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hinoki import terminal
from hinoki.__main__ import main
from hinoki.cli import management as managementcli
from hinoki.cli import project as projectcli
from hinoki.db import Database
from hinoki.orm import migrator
from hinoki.orm.migrations import plan
from hinoki.project import ProjectError, find_manifest, load_manifest
from hinoki.scaffold import create as scaffold


def create(name: str, parent: Path, *, database: str = "none") -> Path:
    """Scaffold a project and register a root app, the layout these tests plan."""
    project = scaffold(name, parent, database=database)
    (project / name / "models.py").write_text(
        '"""Models owned by the project app."""\n', encoding="utf-8"
    )
    migrations = project / name / "migrations"
    migrations.mkdir()
    (migrations / "__init__.py").write_text("", encoding="utf-8")
    manifest = project / "hinoki.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n[[apps]]\n"
        f'name = "{name}"\n'
        f'models = "{name}.models"\n'
        f'migrations = "{name}.migrations"\n'
        "requires = []\n",
        encoding="utf-8",
    )
    return project


def test_manifest_orders_dependencies_and_keeps_declaration_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hinoki.toml"
    path.write_text(
        '[project]\napplication = "sample.main:app"\n\n'
        '[[apps]]\nname = "core"\nmodels = "sample.models"\n'
        'migrations = "sample.migrations"\nrequires = []\n\n'
        '[[apps]]\nname = "billing"\nmodels = "sample.apps.billing.models"\n'
        'migrations = "sample.apps.billing.migrations"\nrequires = ["core"]\n',
        encoding="utf-8",
    )

    manifest = load_manifest(path)

    assert manifest.application == "sample.main:app"
    assert [app.name for app in manifest.ordered_apps()] == ["core", "billing"]
    assert [app.name for app in manifest.ordered_apps("billing")] == [
        "core",
        "billing",
    ]


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        (
            '[[apps]]\nname="core"\nmodels="x.models"\nmigrations="x.migrations"\nrequires=["missing"]\n',
            "unknown app",
        ),
        (
            '[[apps]]\nname="core"\nmodels="x.models"\nmigrations="x.migrations"\nrequires=["other"]\n'
            '[[apps]]\nname="other"\nmodels="x.other"\nmigrations="x.other_migrations"\nrequires=["core"]\n',
            "cycle",
        ),
        (
            '[[apps]]\nname="core"\nmodels="../models"\nmigrations="x.migrations"\n',
            "dotted module",
        ),
    ],
)
def test_manifest_rejects_invalid_dependencies_and_modules(
    tmp_path: Path, apps: str, message: str
) -> None:
    path = tmp_path / "hinoki.toml"
    path.write_text('[project]\napplication = "x.main:app"\n' + apps)

    with pytest.raises(ProjectError, match=message):
        load_manifest(path)


def test_new_project_has_a_manifest_with_no_apps_until_one_is_created(
    tmp_path: Path,
) -> None:
    project = scaffold("sample", tmp_path)

    manifest = load_manifest(project / "hinoki.toml")

    assert manifest.application == "sample.main:app"
    assert manifest.apps == ()
    assert ".hinoki/" in (project / ".gitignore").read_text()

    projectcli.create_app("billing", project)
    created = load_manifest(project / "hinoki.toml").apps[0]
    assert created.models == "sample.apps.billing.models"
    assert created.migrations == "sample.apps.billing.migrations"
    assert (project / "sample/apps/billing/models.py").is_file()
    assert (project / "sample/apps/billing/migrations/__init__.py").is_file()


@pytest.mark.parametrize("database", ["none", "sqlite", "postgresql"])
def test_new_project_ignores_the_dotenv_file_but_not_its_example(
    tmp_path: Path, database: str
) -> None:
    project = create("sample", tmp_path, database=database)

    ignored = (project / ".gitignore").read_text().splitlines()

    assert ".env" in ignored
    assert not any(line.startswith(".env.") or line == ".env*" for line in ignored)


def test_project_migrations_default_to_sqlite_and_initialize_local_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)

    assert main(["migrations", "plan", "--dry-run"]) == 0
    assert not (project / ".hinoki/local.sqlite3").exists()
    assert main(["migrations", "apply"]) == 0
    assert (project / ".hinoki/local.sqlite3").is_file()


def test_project_migrations_use_the_application_settings_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("settingsdb", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    target = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("SETTINGSDB_DATABASE__URL", f"sqlite://{target}")

    assert main(["migrations", "apply"]) == 0
    assert target.is_file()
    assert not (project / ".hinoki/local.sqlite3").exists()
    capsys.readouterr()

    assert main(["migrations", "status"]) == 0
    captured = capsys.readouterr()
    assert (
        f"database: sqlite://{target} (from settings: ProjectSettings.database)"
        in captured.err
    )


def test_project_settings_relative_sqlite_is_rooted_at_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("relativedb", tmp_path, database="sqlite")
    monkeypatch.chdir(project / "relativedb")
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv("RELATIVEDB_DATABASE__URL", "sqlite://./data.sqlite3")

    assert main(["migrations", "apply"]) == 0

    assert (project / "data.sqlite3").is_file()
    assert not (project / "relativedb/data.sqlite3").exists()


@pytest.mark.parametrize("source", ["environment", "argument"])
def test_project_migration_database_override_beats_the_application_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    name = f"override{source}"
    project = create(name, tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    configured = tmp_path / "configured.sqlite3"
    chosen = tmp_path / "chosen.sqlite3"
    monkeypatch.setenv(f"{name.upper()}_DATABASE__URL", f"sqlite://{configured}")
    monkeypatch.setenv("HINOKI_DATABASE_URL", f"sqlite://{chosen}")
    args = ["migrations", "apply"]
    label = "HINOKI_DATABASE_URL"
    if source == "argument":
        environment = tmp_path / "environment.sqlite3"
        monkeypatch.setenv("HINOKI_DATABASE_URL", f"sqlite://{environment}")
        args.extend(["--database", f"sqlite://{chosen}"])
        label = "--database"

    assert main(args) == 0

    assert chosen.is_file()
    assert not configured.exists()
    assert f"database: sqlite://{chosen} (from {label})" in capsys.readouterr().err


@pytest.mark.parametrize(
    "action", [["apply"], ["plan"], ["plan", "--check"], ["status"]]
)
def test_project_migrations_refuse_to_guess_when_settings_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: list[str],
) -> None:
    name = f"broken{''.join(action).strip('-').replace('-', '')}"
    project = create(name, tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv(f"{name.upper()}_DATABASE__MAX_SIZE", "0")

    assert main(["migrations", *action]) == 2

    err = capsys.readouterr().err
    assert "cannot load the application's settings" in err
    assert "fix: " not in err
    assert "--database" in err and "HINOKI_DATABASE_URL" in err
    assert "database: " not in err
    assert not (project / ".hinoki/local.sqlite3").exists()
    assert not list((project / name / "migrations").glob("*.sql"))


def test_an_explicit_database_does_not_need_loadable_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("brokenexplicit", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    target = tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("HINOKI_DATABASE_URL", f"sqlite://{target}")
    monkeypatch.setenv("BROKENEXPLICIT_DATABASE__MAX_SIZE", "0")

    assert main(["migrations", "apply"]) == 0
    assert target.is_file()


def test_project_settings_dotenv_is_read_from_the_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("dotenvdb", tmp_path, database="sqlite")
    target = tmp_path / "dotenv.sqlite3"
    (project / ".env").write_text(
        f"DOTENVDB_DATABASE__URL=sqlite://{target}\n", encoding="utf-8"
    )
    monkeypatch.chdir(project / "dotenvdb")
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.delenv("DOTENVDB_DATABASE__URL", raising=False)

    assert main(["migrations", "plan", "--dry-run"]) == 0
    line = f"database: sqlite://{target} (from settings: ProjectSettings.database)"
    assert line in capsys.readouterr().err
    assert main(["migrations", "apply"]) == 0

    assert target.is_file()
    assert not (project / ".hinoki/local.sqlite3").exists()


def test_project_settings_in_memory_sqlite_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("memorydb", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv("MEMORYDB_DATABASE__URL", "sqlite://:memory:")

    assert main(["migrations", "apply"]) == 2

    err = capsys.readouterr().err
    assert "in-memory SQLite" in err
    assert "fix: " not in err
    assert not (project / ":memory:").exists()


def _replace_application(project: Path, package: str, body: str) -> None:
    (project / package / "main.py").write_text(
        "from hinoki import Hinoki\n"
        "from hinoki.db import DatabaseModule\n"
        "from hinoki.modules import Module\n\n"
        f"from {package}.settings import ProjectSettings\n\n" + body,
        encoding="utf-8",
    )


@pytest.mark.parametrize("form", ["modules", "install"])
def test_project_migrations_use_the_installed_database_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: str,
) -> None:
    name = f"installed{form}"
    project = create(name, tmp_path, database="sqlite")
    installed = tmp_path / "installed.sqlite3"
    configured = tmp_path / "configured.sqlite3"
    module = f'DatabaseModule("sqlite://{installed}")'
    _replace_application(
        project,
        name,
        f"app = Hinoki(settings=ProjectSettings, modules=[{module}])\n"
        if form == "modules"
        else f"app = Hinoki(settings=ProjectSettings)\napp.install({module})\n",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv(f"{name.upper()}_DATABASE__URL", f"sqlite://{configured}")

    assert main(["migrations", "apply"]) == 0

    assert installed.is_file()
    assert not configured.exists()
    line = f"database: sqlite://{installed} (from application: DatabaseModule)"
    assert line in capsys.readouterr().err


@pytest.mark.parametrize(
    "module",
    [
        "class Storage(Module):\n    name = 'database'\n",
        "class Storage(DatabaseModule):\n    def __init__(self) -> None:\n"
        "        pass\n",
    ],
    ids=["other-module", "database-module-without-url"],
)
def test_project_migrations_stop_when_the_database_module_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: str,
) -> None:
    name = f"customdb{uuid4().hex[:10]}"
    project = create(name, tmp_path, database="sqlite")
    _replace_application(
        project,
        name,
        f"{module}\n\napp = Hinoki(settings=ProjectSettings, modules=[Storage()])\n",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)

    assert main(["migrations", "apply"]) == 2

    err = capsys.readouterr().err
    assert "cannot tell which database" in err and "Storage" in err
    assert "--database" in err and "database: " not in err
    assert not (project / ".hinoki/local.sqlite3").exists()


@pytest.mark.parametrize(
    ("application", "named"),
    [("importfail.absent:app", "importfail.absent"), ("importfail.main:nope", "nope")],
)
def test_project_migrations_name_the_application_import_that_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    application: str,
    named: str,
) -> None:
    project = create("importfail", tmp_path, database="sqlite")
    manifest = project / "hinoki.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'application = "importfail.main:app"', f'application = "{application}"'
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)

    assert main(["migrations", "status"]) == 2

    err = capsys.readouterr().err
    assert f"cannot import the application {application}" in err
    assert repr(named) in err and "settings" not in err


def test_project_plan_check_with_an_explicit_database_skips_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("skipapp", tmp_path, database="sqlite")
    (project / "skipapp" / "main.py").write_text(
        "raise RuntimeError('the application must not be imported')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("HINOKI_DATABASE_URL", f"sqlite://{tmp_path / 'skip.sqlite3'}")

    assert main(["migrations", "plan", "--dry-run"]) == 0
    assert main(["migrations", "plan", "--check"]) in {0, 1}


def test_project_application_output_does_not_reach_command_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("noisyapp", tmp_path, database="sqlite")
    main_py = project / "noisyapp" / "main.py"
    main_py.write_text(
        "print('noise from the application')\n" + main_py.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv("NOISYAPP_DATABASE__URL", f"sqlite://{tmp_path / 'n.sqlite3'}")

    assert main(["migrations", "status"]) == 0

    captured = capsys.readouterr()
    assert "noise from the application" not in captured.out
    assert "noise from the application" in captured.err


def test_project_migration_status_never_prints_the_settings_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("secretdb", tmp_path, database="postgresql")
    monkeypatch.chdir(project)
    monkeypatch.delenv("HINOKI_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "SECRETDB_DATABASE__URL",
        "postgres://owner:secret-pw@127.0.0.1:1/none?password=secret-pw",
    )

    assert main(["migrations", "status"]) == 1

    err = capsys.readouterr().err
    assert "database: postgres://***@127.0.0.1:1/none?..." in err
    assert "(from settings: ProjectSettings.database)" in err
    assert "secret-pw" not in err


def test_project_migration_check_fails_on_file_drift_without_database_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("plancheck", tmp_path)
    (project / "plancheck/models.py").write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Note(Model, table='sample_notes'):\n"
        "    id: int = field(primary_key=True)\n"
        "    title: str\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("migration plan check connected to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)
    migration_dir = project / "plancheck/migrations"

    assert main(["migrations", "plan", "--check"]) == 1
    output = capsys.readouterr().out
    assert "create table sample_notes" in output
    assert "pending migrations:" in output
    assert not list(migration_dir.glob("*.sql"))
    assert not list(migration_dir.glob("*.state.json"))
    assert not (project / ".hinoki/local.sqlite3").exists()

    assert main(["migrations", "plan"]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--check"]) == 0
    assert capsys.readouterr().out == "no changes\n"
    assert (project / ".hinoki/local.sqlite3").exists() is False


def test_project_migration_check_covers_all_apps_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("checkmulti", tmp_path)
    (project / "checkmulti/models.py").write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Note(Model, table='checkmulti_notes'):\n"
        "    id: int = field(primary_key=True)\n"
        "    title: str\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("migration plan check connected to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)
    assert main(["app", "create", "billing"]) == 0
    capsys.readouterr()

    assert main(["migrations", "plan", "--check"]) == 1
    output = capsys.readouterr().out
    assert "checkmulti:" in output and "billing:" in output
    assert "pending migrations: 1 operation across apps" in output

    assert main(["migrations", "plan", "--app", "checkmulti", "--check"]) == 1
    assert "pending migrations:" in capsys.readouterr().out
    assert main(["migrations", "plan", "--app", "billing", "--check"]) == 0
    assert capsys.readouterr().out == "no changes\n"
    assert not (project / ".hinoki/local.sqlite3").exists()


def test_invalid_project_app_does_not_create_local_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)

    assert main(["migrations", "status", "--app", "missing"]) == 1
    assert "unknown app" in capsys.readouterr().err
    assert not (project / ".hinoki/local.sqlite3").exists()


def test_app_command_creates_registers_and_discovery_walks_to_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    nested = project / "sample" / "apps" / "existing"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert main(["app", "create", "billing"]) == 0
    output = capsys.readouterr().out
    assert "created app billing" in output and "registered in" in output
    manifest_path = find_manifest()
    manifest = load_manifest(manifest_path)
    billing = next(app for app in manifest.apps if app.name == "billing")
    assert billing.models == "sample.apps.billing.models"
    assert billing.migrations == "sample.apps.billing.migrations"
    migration_init = manifest_path.parent / "sample/apps/billing/migrations/__init__.py"
    assert migration_init.is_file()


def test_app_list_is_dependency_ordered_and_does_not_import_apps_or_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    (project / "hinoki.toml").write_text(
        '[project]\napplication = "sample.main:app"\n'
        '[[apps]]\nname = "billing"\nmodels = "sample.apps.billing.models"\n'
        'migrations = "sample.apps.billing.migrations"\nrequires = ["accounts"]\n'
        '[[apps]]\nname = "accounts"\nmodels = "sample.apps.accounts.models"\n'
        'migrations = "sample.apps.accounts.migrations"\nrequires = []\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(project / "sample")

    def fail_import(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("app list imported app code")

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("app list connected to a database")

    monkeypatch.setattr("hinoki.project.importlib.import_module", fail_import)
    monkeypatch.setattr(Database, "connect", fail_connect)

    assert main(["app", "list"]) == 0
    output = capsys.readouterr().out
    assert output.index("accounts\n") < output.index("billing\n")
    assert "models: sample.apps.billing.models" in output
    assert "migrations: sample.apps.billing.migrations" in output
    assert "requires: accounts" in output


def test_app_list_json_has_stable_manifest_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)

    assert main(["app", "list", "--format", "json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "apps": [
            {
                "name": "sample",
                "models": "sample.models",
                "migrations": "sample.migrations",
                "requires": [],
                "events": "",
            }
        ]
    }


def test_app_list_empty_manifest_and_missing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "hinoki.toml").write_text(
        '[project]\napplication = "sample.main:app"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    assert main(["app", "list"]) == 0
    assert capsys.readouterr().out == "no apps registered\n"
    assert main(["app", "list", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"apps": []}

    missing = tmp_path.parent / f"{tmp_path.name}-missing"
    missing.mkdir()
    monkeypatch.chdir(missing)
    assert main(["app", "list"]) == 2
    assert capsys.readouterr().err == (
        "error: no hinoki.toml found; run this command from the project directory\n"
    )


def test_manage_uses_the_manifest_application_from_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = create("sample", tmp_path)
    nested = project / "sample"
    monkeypatch.chdir(nested)

    assert managementcli.run([]) == 0
    assert "this app has no management commands" in capsys.readouterr().out


TARGET_HANDLERS = {
    "check": "hinoki.__main__.run_check",
    "dev": "hinoki.serve.run_dev",
    "events": "hinoki.cli.events.run_events",
    "mcp": "hinoki.__main__.run_mcp_command",
    "openapi": "hinoki.cli.openapi.run_openapi",
    "routes": "hinoki.cli.routes.run_routes",
    "serve": "hinoki.serve.run_serve",
    "settings": "hinoki.cli.settings.run_settings",
}


def _record_targets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def record(args: argparse.Namespace) -> int:
        seen.append(args.target)
        return 0

    for handler in TARGET_HANDLERS.values():
        monkeypatch.setattr(handler, record)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return seen


@pytest.mark.parametrize("command", sorted(TARGET_HANDLERS))
def test_app_commands_default_to_the_manifest_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project / "sample")
    seen = _record_targets(monkeypatch)

    assert main([command]) == 0
    assert seen == ["sample.main:app"]
    assert str(project.resolve()) in sys.path


@pytest.mark.parametrize("command", sorted(TARGET_HANDLERS))
def test_app_commands_need_a_target_outside_a_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    seen = _record_targets(monkeypatch)

    assert main([command]) == 2
    assert seen == []
    assert capsys.readouterr().err == (
        "error: pass TARGET outside a project with hinoki.toml\n"
    )


def test_an_explicit_target_wins_over_the_manifest_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)
    seen = _record_targets(monkeypatch)

    for command in TARGET_HANDLERS:
        assert main([command, "other.main:create_app"]) == 0
    assert seen == ["other.main:create_app"] * len(TARGET_HANDLERS)


def test_routes_without_a_target_load_the_project_app_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = f"routed{uuid4().hex[:10]}"
    project = create(name, tmp_path)
    monkeypatch.chdir(project / name)
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert main(["routes"]) == 0
    assert "/health" in capsys.readouterr().out


def test_app_command_refuses_duplicates_and_preserves_existing_files(
    tmp_path: Path,
) -> None:
    project = create("sample", tmp_path)
    existing = project / "sample" / "apps" / "billing"
    existing.mkdir(parents=True)
    keep = existing / "keep.py"
    keep.write_text("user content")
    before = (project / "hinoki.toml").read_bytes()

    with pytest.raises(ProjectError, match="already exists"):
        projectcli.create_app("billing", project)

    assert keep.read_text() == "user content"
    assert (project / "hinoki.toml").read_bytes() == before
    assert not (project / ".hinoki-app-create.lock").exists()


def test_app_creation_removes_its_lock_file(tmp_path: Path) -> None:
    project = create("sample", tmp_path)

    projectcli.create_app("billing", project)

    assert (project / "sample" / "apps" / "billing").is_dir()
    assert not (project / ".hinoki-app-create.lock").exists()


def test_concurrent_app_creation_preserves_both_manifest_entries(
    tmp_path: Path,
) -> None:
    project = create("sample", tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(
            executor.map(
                lambda name: projectcli.create_app(name, project),
                ("billing", "search"),
            )
        )

    assert all(path.is_dir() for path in created)
    assert {app.name for app in load_manifest(project / "hinoki.toml").apps} == {
        "sample",
        "billing",
        "search",
    }
    assert not (project / ".hinoki-app-create.lock").exists()


def test_failed_manifest_replace_removes_only_the_new_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("sample", tmp_path)
    manifest = project / "hinoki.toml"
    before = manifest.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hinoki.cli.project.os.replace", fail_replace)
    with pytest.raises(ProjectError, match="could not create app"):
        projectcli.create_app("billing", project)

    assert manifest.read_bytes() == before
    assert not (project / "sample/apps/billing").exists()


def test_app_migrations_validate_and_include_cross_app_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hinoki.toml"
    path.write_text(
        '[project]\napplication = "sample.main:app"\n'
        '[[apps]]\nname = "accounts"\nmodels = "tests.projects.workflowaccounts"\n'
        'migrations = "tests.projects.workflowaccounts"\nrequires = []\n'
        '[[apps]]\nname = "billing"\nmodels = "tests.projects.workflowbilling"\n'
        'migrations = "tests.projects.workflowbilling"\nrequires = ["accounts"]\n',
        encoding="utf-8",
    )
    manifest = load_manifest(path)

    models = manifest.all_models()
    billing_models = models["billing"]
    accounts_models = models["accounts"]
    planned = plan(
        billing_models,
        tmp_path / "billing-migrations",
        "initial",
        write=False,
        reference_models=accounts_models,
    )

    assert set(planned.state["tables"]) == {"workflow_invoices"}
    assert 'REFERENCES "workflow_accounts"' in planned.operations[0].sql


def test_cross_app_reference_requires_dependency_registration(tmp_path: Path) -> None:
    path = tmp_path / "hinoki.toml"
    path.write_text(
        '[project]\napplication = "sample.main:app"\n'
        '[[apps]]\nname = "accounts"\nmodels = "tests.projects.workflowaccounts"\n'
        'migrations = "tests.projects.workflowaccounts"\nrequires = []\n'
        '[[apps]]\nname = "billing"\nmodels = "tests.projects.workflowbilling"\n'
        'migrations = "tests.projects.workflowbilling"\nrequires = []\n',
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="add 'accounts' to its requires list"):
        load_manifest(path).all_models()


def _modular_project(tmp_path: Path) -> tuple[Path, str, Path]:
    name = f"safety{uuid4().hex[:10]}"
    project = create(name, tmp_path)
    (project / name / "models.py").write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Account(Model, table='accounts'):\n"
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    projectcli.create_app("billing", project)
    manifest = project / "hinoki.toml"
    text = manifest.read_text(encoding="utf-8")
    billing_section = text.index('name = "billing"')
    text = text[:billing_section] + text[billing_section:].replace(
        "requires = []", f'requires = ["{name}"]', 1
    )
    manifest.write_text(text, encoding="utf-8")
    billing_models = project / name / "apps" / "billing" / "models.py"
    billing_models.write_text(
        f"from hinoki.orm import Model, OnDelete, field, foreign_key\n"
        f"from {name}.models import Account\n\n"
        "class Invoice(Model, table='invoices'):\n"
        "    id: int = field(primary_key=True)\n"
        "    account_id: int = foreign_key(Account, on_delete=OnDelete.RESTRICT)\n",
        encoding="utf-8",
    )
    return project, name, billing_models


def _clear_project_model_modules(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, f"{name}.models", raising=False)
    monkeypatch.delitem(sys.modules, f"{name}.apps.billing.models", raising=False)


def test_plan_prints_the_written_path_relative_to_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _ = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    _clear_project_model_modules(name, monkeypatch)

    assert main(["migrations", "plan", "--app", name]) == 0
    out = capsys.readouterr().out
    assert f"wrote {name}/migrations/0001-" in out
    assert str(tmp_path) not in out


def _shown_from(monkeypatch: pytest.MonkeyPatch, here: Path, home: Path) -> None:
    """Show paths as if the working directory and home were elsewhere, while
    the project itself is still found from the real working directory."""
    fake = SimpleNamespace(cwd=lambda: here, home=lambda: home)
    monkeypatch.setattr(terminal, "Path", fake)


def test_plan_keeps_an_absolute_path_outside_the_working_directory_and_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _ = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    _clear_project_model_modules(name, monkeypatch)
    _shown_from(monkeypatch, tmp_path / "elsewhere", tmp_path / "home")

    assert main(["migrations", "plan", "--app", name]) == 0
    out = capsys.readouterr().out
    assert f"wrote {project / name / 'migrations'}{os.sep}0001-" in out


def test_plan_shortens_a_path_under_home_with_a_tilde(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _ = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    _clear_project_model_modules(name, monkeypatch)
    _shown_from(monkeypatch, tmp_path / "elsewhere", tmp_path)

    assert main(["migrations", "plan", "--app", name]) == 0
    out = capsys.readouterr().out
    assert f"wrote ~{os.sep}{project.name}{os.sep}{name}{os.sep}migrations" in out
    assert str(tmp_path) not in out


def test_plan_shortens_a_path_under_a_symlinked_home_with_a_tilde(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _ = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    _clear_project_model_modules(name, monkeypatch)
    home = tmp_path / "home-link"
    home.symlink_to(tmp_path, target_is_directory=True)
    _shown_from(monkeypatch, tmp_path / "elsewhere", home)

    assert main(["migrations", "plan", "--app", name]) == 0
    out = capsys.readouterr().out
    assert f"wrote ~{os.sep}{project.name}{os.sep}{name}{os.sep}migrations" in out


def test_plain_app_create_shows_paths_relative_to_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)

    assert main(["app", "create", "billing"]) == 0
    output = capsys.readouterr().out
    created = Path("sample", "apps", "billing")
    assert f"created app billing at {created}\nregistered in hinoki.toml" in output


def test_focused_child_plan_requires_recorded_parent_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    billing_dir = project / _name / "apps" / "billing" / "migrations"

    assert main(["migrations", "plan", "--app", "billing"]) == 1
    error = capsys.readouterr().err
    assert "absent from recorded migrations" in error
    assert f"plan {_name} first" in error
    assert not list(billing_dir.glob("*.sql"))
    assert not list(billing_dir.glob("*.state.json"))

    assert main(["migrations", "plan", "--app", _name]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--app", "billing"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert len(list(billing_dir.glob("*.sql"))) == 1


def test_multi_app_offline_preview_checks_and_summarizes_all_apps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("offline previews must not connect to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)

    assert main(["migrations", "plan", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert f"{name}:" in output
    assert "billing:" in output
    assert "create table accounts" in output
    assert "create table invoices" in output
    assert "all-app preview only" in output
    assert not list(project.rglob("*.sql"))
    assert not list(project.rglob("*.state.json"))

    assert main(["migrations", "plan", "--check"]) == 1
    output = capsys.readouterr().out
    assert f"{name}:" in output and "billing:" in output
    assert "pending migrations:" in output
    assert not (project / ".hinoki/local.sqlite3").exists()

    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--app", "billing"]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--check"]) == 0
    assert capsys.readouterr().out.endswith("no migrations are pending\n")


def test_multi_app_preview_rejects_name_as_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)

    assert main(["migrations", "plan", "--dry-run", "--name", "preview"]) == 1
    assert "--name requires --app for a multi-app preview" in capsys.readouterr().err


def test_multi_app_plan_without_an_app_names_the_positional_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)

    assert main(["migrations", "plan"]) == 1
    error = capsys.readouterr().err
    assert f"multiple apps ({name}, billing)" in error
    assert "`hinoki migrations plan <app>`" in error
    assert "inspect with `hinoki migrations plan`" not in error
    assert not list(project.rglob("*.sql"))


def test_migration_sql_searches_every_app_and_refuses_an_ambiguous_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", name, "--name", "shared"]) == 0
    assert main(["migrations", "plan", "billing", "--name", "shared"]) == 0
    billing_dir = project / name / "apps" / "billing" / "migrations"
    (billing_dir / "0002-billing-only.sql").write_text(
        "-- billing only\n", encoding="utf-8"
    )
    capsys.readouterr()

    assert main(["migrations", "sql", "0002-billing-only"]) == 0
    assert capsys.readouterr().out == "-- billing only\n"

    assert main(["migrations", "sql", "0001-shared"]) == 1
    error = capsys.readouterr().err
    assert f"exists in apps {name}, billing" in error and "--app" in error

    assert main(["migrations", "sql", "0001-shared", "--app", "billing"]) == 0
    assert "create table invoices" in capsys.readouterr().out.lower()

    assert main(["migrations", "sql", "9999-missing"]) == 1
    assert "no app has a migration named '9999-missing'" in capsys.readouterr().err


def test_project_dry_run_shows_destructive_changes_without_write_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("dryrun", tmp_path)
    model_file = project / "dryrun/models.py"
    model_file.write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Note(Model, table='dryrun_notes'):\n"
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", "--app", "dryrun"]) == 0
    capsys.readouterr()
    migration_dir = project / "dryrun/migrations"
    before = {
        path.name: path.read_bytes()
        for path in migration_dir.iterdir()
        if path.suffix in {".sql", ".json"}
    }

    model_file.write_text("from hinoki.orm import Model\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "dryrun.models", raising=False)
    assert main(["migrations", "plan", "--app", "dryrun", "--dry-run"]) == 0
    assert "DESTRUCTIVE drop table dryrun_notes" in capsys.readouterr().out
    assert {
        path.name: path.read_bytes()
        for path in migration_dir.iterdir()
        if path.suffix in {".sql", ".json"}
    } == before


@pytest.mark.parametrize("option", ["--dry-run", "--check"])
def test_all_app_preview_discards_partial_output_when_teardown_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
) -> None:
    project, name, billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--app", "billing"]) == 0
    capsys.readouterr()

    billing_models.write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Invoice(Model, table='invoices'):\n"
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    _clear_project_model_modules(name, monkeypatch)
    assert main(["migrations", "plan", "--app", "billing", "--allow-destructive"]) == 0
    capsys.readouterr()

    (project / name / "models.py").write_text(
        "from hinoki.orm import Model, field\n",
        encoding="utf-8",
    )
    _clear_project_model_modules(name, monkeypatch)
    assert main(["migrations", "plan", option]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "recorded migrations for app 'billing' reference" in captured.err


def test_bare_migrations_command_prints_workflow_help_without_io(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("group help must not connect to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)
    monkeypatch.setattr(
        "hinoki.__main__.models_of",
        lambda _module: (_ for _ in ()).throw(AssertionError("models imported")),
    )

    assert main(["migrations"]) == 0
    output = capsys.readouterr().out
    assert "plan" in output and "apply" in output and "status" in output
    assert "workflow:" in output.lower()


def test_focused_child_plan_normalizes_legacy_sqlite_uuid_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    account_models = project / name / "models.py"
    account_models.write_text(
        "from uuid import UUID\nfrom hinoki.orm import Model, field\n\n"
        "class Account(Model, table='accounts'):\n"
        "    id: UUID = field(primary_key=True)\n",
        encoding="utf-8",
    )
    billing_models.write_text(
        "from uuid import UUID\n"
        "from hinoki.orm import Model, OnDelete, field, foreign_key\n"
        f"from {name}.models import Account\n\n"
        "class Invoice(Model, table='invoices'):\n"
        "    id: int = field(primary_key=True)\n"
        "    account_id: UUID = foreign_key(\n"
        "        Account, on_delete=OnDelete.RESTRICT\n"
        "    )\n",
        encoding="utf-8",
    )
    _clear_project_model_modules(name, monkeypatch)

    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()
    account_state = next((project / name / "migrations").glob("*.state.json"))
    state = json.loads(account_state.read_text(encoding="utf-8"))
    state["tables"]["accounts"]["columns"]["id"]["type"] = "UUID"
    account_state.write_text(json.dumps(state), encoding="utf-8")

    assert main(["migrations", "plan", "--app", "billing"]) == 0
    assert "wrote" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["type", "primary-key-column"])
def test_focused_child_plan_rejects_unrecorded_parent_target_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    project, name, billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()

    if change == "type":
        (project / name / "models.py").write_text(
            "from hinoki.orm import Model, field\n\n"
            "class Account(Model, table='accounts'):\n"
            "    id: float = field(primary_key=True)\n",
            encoding="utf-8",
        )
        billing_models.write_text(
            f"from hinoki.orm import Model, OnDelete, field, foreign_key\n"
            f"from {name}.models import Account\n\n"
            "class Invoice(Model, table='invoices'):\n"
            "    id: int = field(primary_key=True)\n"
            "    account_id: float = foreign_key(\n"
            "        Account, on_delete=OnDelete.RESTRICT\n"
            "    )\n",
            encoding="utf-8",
        )
        message = "incompatible with recorded target"
    else:
        (project / name / "models.py").write_text(
            "from hinoki.orm import Model, field\n\n"
            "class Account(Model, table='accounts'):\n"
            "    key: int = field(primary_key=True)\n",
            encoding="utf-8",
        )
        message = "absent from recorded migrations"

    _clear_project_model_modules(name, monkeypatch)
    assert main(["migrations", "plan", "--app", "billing"]) == 1
    assert message in capsys.readouterr().err
    assert len(list((project / name / "apps/billing/migrations").glob("*.sql"))) == 0


def test_parent_teardown_checks_all_child_history_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()
    assert main(["migrations", "plan", "--app", "billing"]) == 0
    capsys.readouterr()

    billing_models.write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Invoice(Model, table='invoices'):\n"
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    (project / name / "models.py").write_text(
        "from hinoki.orm import Model, field\n\n"
        "class Account(Model, table='accounts'):\n"
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    _clear_project_model_modules(name, monkeypatch)
    assert main(["migrations", "plan", "--app", "billing", "--allow-destructive"]) == 0
    capsys.readouterr()

    (project / name / "models.py").write_text(
        "from hinoki.orm import Model, field\n",
        encoding="utf-8",
    )
    _clear_project_model_modules(name, monkeypatch)
    parent_dir = project / name / "migrations"
    before = set(parent_dir.iterdir())
    assert main(["migrations", "plan", "--app", name, "--allow-destructive"]) == 1
    error = capsys.readouterr().err
    assert "recorded migrations for app 'billing' reference" in error
    assert "separate reviewed deployment phase" in error
    assert set(parent_dir.iterdir()) == before


@pytest.mark.parametrize("layout", ["same-package", "symlink", "nested"])
def test_project_migrations_reject_shared_or_overlapping_directories_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layout: str,
) -> None:
    project, name, _billing_models = _modular_project(tmp_path)
    manifest = project / "hinoki.toml"
    text = manifest.read_text(encoding="utf-8")
    if layout == "same-package":
        text = text.replace(
            f'migrations = "{name}.apps.billing.migrations"',
            f'migrations = "{name}.migrations"',
        )
    elif layout == "symlink":
        (project / name / "migrations_alias").symlink_to(
            "migrations", target_is_directory=True
        )
        text = text.replace(
            f'migrations = "{name}.apps.billing.migrations"',
            f'migrations = "{name}.migrations_alias"',
        )
    else:
        nested = project / name / "migrations" / "nested"
        nested.mkdir()
        (nested / "__init__.py").write_text("", encoding="utf-8")
        text = text.replace(
            f'migrations = "{name}.apps.billing.migrations"',
            f'migrations = "{name}.migrations.nested"',
        )
    manifest.write_text(text, encoding="utf-8")
    monkeypatch.chdir(project)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("directory validation must precede database connect")

    monkeypatch.setattr(Database, "connect", fail_connect)
    assert main(["migrations", "status"]) == 1
    error = capsys.readouterr().err
    assert "same or overlapping migrations directories" in error
    assert name in error and "billing" in error


def test_project_migration_history_fails_closed_on_malformed_older_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, name, _billing_models = _modular_project(tmp_path)
    monkeypatch.chdir(project)
    assert main(["migrations", "plan", "--app", name]) == 0
    capsys.readouterr()
    accounts_dir = project / name / "migrations"
    (accounts_dir / "0000-old.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (accounts_dir / "0000-old.state.json").write_text("{bad", encoding="utf-8")

    assert main(["migrations", "plan", "--app", "billing"]) == 1
    error = capsys.readouterr().err
    assert "0000-old.state.json is not valid JSON" in error
    assert not list((project / name / "apps/billing/migrations").glob("*.sql"))


async def test_app_migrations_with_same_filename_have_separate_sqlite_ledger_entries(
    tmp_path: Path,
) -> None:
    database = await Database.connect(f"sqlite:///{tmp_path / 'local.db'}", max_size=1)
    accounts = tmp_path / "accounts"
    billing = tmp_path / "billing"
    root_app = tmp_path / "root_app"
    root_app.mkdir()
    accounts.mkdir()
    billing.mkdir()
    (root_app / "0001-initial.sql").write_text(
        "CREATE TABLE workflow_root (id integer PRIMARY KEY);\n"
    )
    (accounts / "0001-initial.sql").write_text(
        "CREATE TABLE workflow_account (id integer PRIMARY KEY);\n"
    )
    (billing / "0001-initial.sql").write_text(
        "CREATE TABLE workflow_invoice (id integer PRIMARY KEY);\n"
    )

    try:
        assert await migrator.apply(database, root_app) == ["0001-initial"]
        assert await migrator.apply(database, accounts, namespace="accounts") == [
            "0001-initial"
        ]
        assert await migrator.apply(database, billing, namespace="billing") == [
            "0001-initial"
        ]
        assert await migrator.status(database, accounts, namespace="accounts") == [
            ("0001-initial", "applied")
        ]
        assert await migrator.status(database, billing, namespace="billing") == [
            ("0001-initial", "applied")
        ]
        assert await migrator.status(database, root_app) == [
            ("0001-initial", "applied")
        ]
        rows = await database.fetch("SELECT name FROM hinoki_migrations ORDER BY name")
        assert [row["name"] for row in rows] == [
            "0001-initial",
            "accounts:0001-initial",
            "billing:0001-initial",
        ]
    finally:
        database.close()


def test_a_new_project_serves_its_routes_from_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("routedsample", tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    for name in [key for key in sys.modules if key.startswith("routedsample")]:
        monkeypatch.delitem(sys.modules, name)
    manifest = (project / "hinoki.toml").read_text(encoding="utf-8")
    assert 'routes = "routedsample.routes:routes"' in manifest
    main_text = (project / "routedsample" / "main.py").read_text(encoding="utf-8")
    assert "include=" not in main_text
    assert "manifest_routes" not in main_text

    app = importlib.import_module("routedsample.main").app
    assert [route.path for route in app.routes] == ["/health"]


def test_app_create_scaffolds_reusable_routes_and_explicit_mount_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("billedsample", tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    for name in [key for key in sys.modules if key.startswith("billedsample")]:
        monkeypatch.delitem(sys.modules, name)

    assert main(["app", "create", "billing"]) == 0
    output = capsys.readouterr().out
    assert 'Mount("/billing", billing_routes)' in output
    app_directory = project / "billedsample" / "apps" / "billing"
    routes_file = app_directory / "routes.py"
    routes_text = routes_file.read_text(encoding="utf-8")
    assert "routes: list[Includable] = []" in routes_text
    assert "billedsample/routes.py" in routes_text
    assert "# from billedsample.apps.billing.api import" in routes_text
    assert "RouteGroup(" in (app_directory / "api.py").read_text(encoding="utf-8")
    manifest = (project / "hinoki.toml").read_text(encoding="utf-8")
    assert "routes =" not in manifest.split("[[apps]]", 1)[1]
    assert "prefix =" not in manifest.split("[[apps]]", 1)[1]

    # Creating an app does not expose it until project routes.py mounts it.
    app = importlib.import_module("billedsample.main").app
    assert [route.path for route in app.routes] == ["/health"]
    for name in [key for key in sys.modules if key.startswith("billedsample")]:
        monkeypatch.delitem(sys.modules, name)

    routes_file.write_text(
        "from hinoki import http\n\n\n"
        '@http.get("/invoices")\ndef invoices() -> list[str]:\n    return []\n\n\n'
        "routes = [invoices]\n",
        encoding="utf-8",
    )
    (project / "billedsample" / "routes.py").write_text(
        "from hinoki import Includable, Mount\n"
        "from billedsample.api import health\n"
        "from billedsample.apps.billing.routes import routes as billing_routes\n\n"
        'routes: list[Includable] = [health, Mount("/payments", billing_routes)]\n',
        encoding="utf-8",
    )
    app = importlib.import_module("billedsample.main").app
    assert [route.path for route in app.routes] == ["/health", "/payments/invoices"]

    capsys.readouterr()
    assert main(["app", "list"]) == 0
    listing = capsys.readouterr().out
    assert "models: billedsample.apps.billing.models" in listing
    assert "routes:" not in listing
    assert main(["app", "list", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)["apps"]
    assert [(row["name"], row["models"]) for row in rows] == [
        ("billedsample", "billedsample.models"),
        ("billing", "billedsample.apps.billing.models"),
    ]


def test_app_create_takes_no_models_and_requires_from_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)
    assert main(["app", "create", "billing"]) == 0
    args = ["app", "create", "gateway", "--no-models", "--requires", "billing"]
    assert main(args) == 0
    capsys.readouterr()

    gateway = next(app for app in load_manifest().apps if app.name == "gateway")
    assert not gateway.has_persistence
    assert (gateway.models, gateway.migrations, gateway.requires) == (
        "",
        "",
        ("billing",),
    )
    gateway_dir = project / "sample" / "apps" / "gateway"
    assert (gateway_dir / "api.py").is_file()
    assert (gateway_dir / "routes.py").is_file()
    assert not (gateway_dir / "models.py").exists()
    assert not (gateway_dir / "migrations").exists()

    assert main(["app", "create", "bad", "--requires", "nope"]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_app_create_without_a_name_needs_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(create("sample", tmp_path))
    assert main(["app", "create"]) == 2
    assert "app name is required" in capsys.readouterr().err


def test_app_create_asks_for_what_is_missing_in_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(create("sample", tmp_path))
    assert main(["app", "create", "billing"]) == 0
    monkeypatch.setattr(projectcli, "interactive", lambda _args: True)
    answers = iter(["invoices", "billing"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert main(["app", "create"]) == 0
    capsys.readouterr()
    invoices = next(app for app in load_manifest().apps if app.name == "invoices")
    assert invoices.requires == ("billing",)


def test_cancelling_the_app_questions_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(create("sample", tmp_path))
    monkeypatch.setattr(projectcli, "interactive", lambda _args: True)

    def cancel(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", cancel)

    assert main(["app", "create"]) == 130
    assert "invoices" not in capsys.readouterr().out
    assert [app.name for app in load_manifest().apps] == ["sample"]
