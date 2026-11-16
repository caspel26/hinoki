"""Project registry and modular-app commands."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from hinoki import managementcli, projectcli
from hinoki.__main__ import main
from hinoki.db import Database
from hinoki.orm import migrator
from hinoki.orm.migrations import plan
from hinoki.project import ProjectError, find_manifest, load_manifest
from hinoki.scaffold import create


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


def test_new_project_has_manifest_and_default_migration_package(
    tmp_path: Path,
) -> None:
    project = create("sample", tmp_path)

    manifest = load_manifest(project / "hinoki.toml")

    assert manifest.application == "sample.main:app"
    assert manifest.apps[0].models == "sample.models"
    assert manifest.apps[0].migrations == "sample.migrations"
    assert manifest.migration_namespace(manifest.apps[0]) == ""
    assert (project / "sample/models.py").is_file()
    assert (project / "sample/migrations/__init__.py").is_file()
    assert ".hinoki/" in (project / ".gitignore").read_text()


def test_project_migrations_default_to_sqlite_and_initialize_local_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("sample", tmp_path)
    monkeypatch.chdir(project)

    assert main(["migrations", "plan", "--dry-run"]) == 0
    assert not (project / ".hinoki/local.sqlite3").exists()
    assert main(["migrations", "apply"]) == 0
    assert (project / ".hinoki/local.sqlite3").is_file()


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


def test_project_migration_check_requires_each_app_in_multi_app_projects(
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
    assert (
        "pass --app because this project has multiple apps" in capsys.readouterr().err
    )

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
    assert "created app billing" in capsys.readouterr().out
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


def test_failed_manifest_replace_removes_only_the_new_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("sample", tmp_path)
    manifest = project / "hinoki.toml"
    before = manifest.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hinoki.projectcli.os.replace", fail_replace)
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
