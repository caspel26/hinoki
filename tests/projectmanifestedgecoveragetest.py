"""Manifest validation and import-boundary behavior."""

from __future__ import annotations

import types
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from hinoki import project
from hinoki.orm import Model, field
from hinoki.project import ProjectApp, ProjectError, ProjectManifest


def _manifest(root: Path, apps: tuple[ProjectApp, ...] = ()) -> ProjectManifest:
    path = root / "hinoki.toml"
    path.touch()
    return ProjectManifest(path, "sample.main:app", apps)


def test_project_manifest_app_resolution_requires_unambiguous_selection(
    tmp_path: Path,
) -> None:
    empty = _manifest(tmp_path)
    with pytest.raises(ProjectError, match="pass --app"):
        empty.app()
    with pytest.raises(ProjectError, match="unknown app"):
        empty.app("missing")

    app = ProjectApp("sample", "sample.models", "sample.migrations", ())
    single = _manifest(tmp_path, (app,))
    assert single.app() == app
    assert single.app("sample") == app

    second = ProjectApp("billing", "sample.billing", "sample.billing_migrations", ())
    with pytest.raises(ProjectError, match="pass --app"):
        _manifest(tmp_path, (app, second)).app()


def test_manifest_migrations_path_translates_import_and_nonpackage_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    app = ProjectApp("sample", "sample.models", "sample.migrations", ())
    import_module = types.ModuleType

    def import_failure(_name: str) -> types.ModuleType:
        raise ImportError("package missing")

    monkeypatch.setitem(
        project.__dict__, "importlib", SimpleNamespace(import_module=import_failure)
    )
    with pytest.raises(ProjectError, match="cannot import migration package"):
        manifest.migrations_path(app)

    monkeypatch.setitem(
        project.__dict__,
        "importlib",
        SimpleNamespace(import_module=lambda _name: import_module("plain")),
    )
    with pytest.raises(ProjectError, match="must name a Python package"):
        manifest.migrations_path(app)

    package = types.ModuleType("package")
    package.__path__ = [str(tmp_path)]
    monkeypatch.setitem(
        project.__dict__,
        "importlib",
        SimpleNamespace(import_module=lambda _name: package),
    )
    assert manifest.migrations_path(app) == tmp_path


def test_manifest_migration_directories_reject_resolution_file_and_alias_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ProjectApp("first", "sample.first", "first.migrations", ())
    second = ProjectApp("second", "sample.second", "second.migrations", ())
    manifest = _manifest(tmp_path, (first, second))
    first_path = tmp_path / "first-migrations"
    second_path = tmp_path / "second-migrations"
    first_path.mkdir()
    second_path.mkdir()

    def path_for(app: ProjectApp) -> Path:
        return first_path if app.name == "first" else second_path

    monkeypatch.setattr(
        ProjectManifest, "migrations_path", lambda _self, app: path_for(app)
    )
    assert manifest.migration_directories() == {
        "first": first_path,
        "second": second_path,
    }

    monkeypatch.setattr(
        ProjectManifest,
        "migrations_path",
        lambda _self, _app: tmp_path / "not-created",
    )
    with pytest.raises(ProjectError, match="cannot resolve migrations directory"):
        manifest.migration_directories()

    file_path = tmp_path / "not-a-directory"
    file_path.touch()
    monkeypatch.setattr(
        ProjectManifest, "migrations_path", lambda _self, _app: file_path
    )
    with pytest.raises(ProjectError, match="is not a directory"):
        manifest.migration_directories()

    monkeypatch.setattr(
        ProjectManifest,
        "migrations_path",
        lambda _self, app: first_path if app.name == "first" else first_path / "nested",
    )
    (first_path / "nested").mkdir()
    with pytest.raises(ProjectError, match="same or overlapping"):
        manifest.migration_directories()

    def fail_samefile(_left: Path, _right: Path) -> bool:
        raise OSError("filesystem comparison failed")

    monkeypatch.setattr("hinoki.project.os.path.samefile", fail_samefile)
    with pytest.raises(ProjectError, match="cannot compare registered"):
        project._migration_directories_conflict(first_path, second_path)


def test_models_wraps_missing_modules_and_nonmodel_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    app = ProjectApp("sample", "sample.models", "sample.migrations", ())

    def missing_module(_name: str) -> list[type[Model]]:
        raise ImportError("not found")

    monkeypatch.setattr("hinoki.project.models_of", missing_module)
    with pytest.raises(ProjectError, match="cannot import models for app"):
        manifest.models(app)

    def missing_attribute(_name: str) -> list[type[Model]]:
        raise AttributeError("models is absent")

    monkeypatch.setattr("hinoki.project.models_of", missing_attribute)
    with pytest.raises(ProjectError, match="models is absent"):
        manifest.models(app)


def test_all_models_rejects_ledger_and_duplicate_table_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = ProjectApp("first", "sample.first", "first.migrations", ())
    second = ProjectApp("second", "sample.second", "second.migrations", ())
    manifest = _manifest(tmp_path, (first, second))

    class Ledger(Model, table="hinoki_migrations"):
        id: int = field(primary_key=True)

    monkeypatch.setattr(
        ProjectManifest, "models", lambda _self, app: [Ledger] if app == first else []
    )
    with pytest.raises(ProjectError, match="migration ledger table"):
        manifest.all_models()

    class First(Model, table="shared_project_table"):
        id: int = field(primary_key=True)

    class Second(Model, table="shared_project_table"):
        id: int = field(primary_key=True)

    monkeypatch.setattr(
        ProjectManifest,
        "models",
        lambda _self, app: [First] if app == first else [Second],
    )
    with pytest.raises(ProjectError, match="declared by"):
        manifest.all_models()


def test_reference_owner_rejects_unregistered_table_and_dependency() -> None:
    class Invoice:
        __name__ = "Invoice"

    class Account:
        __table__ = "accounts"

    with pytest.raises(ProjectError, match=r"not registered in hinoki\.toml"):
        project._check_reference_owner(
            cast(type[Model], Invoice),
            "account_id",
            cast(type[Model], Account),
            {},
            set(),
            "billing",
        )
    with pytest.raises(ProjectError, match="add 'accounts' to its requires list"):
        project._check_reference_owner(
            cast(type[Model], Invoice),
            "account_id",
            cast(type[Model], Account),
            {"accounts": "accounts"},
            {"billing"},
            "billing",
        )


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("", r"needs a \[project\] table"),
        ("[project]\napplication = 'bad target'", "module:attribute target"),
        ("apps = {}\n[project]\napplication = 'sample.main:app'", "array of"),
        (
            "[project]\napplication = 'sample.main:app'\n"
            "[[apps]]\nname='sample'\nmodels='sample.models'\nmigrations='sample.migrations'\n"
            "[[apps]]\nname='sample'\nmodels='sample.other'\nmigrations='sample.other_migrations'\n",
            "app names must be unique",
        ),
        (
            "[project]\napplication = 'sample.main:app'\n"
            "[[apps]]\nname='sample'\nmodels='sample.models'\nmigrations='sample.migrations'\nrequires=['missing']\n",
            "requires unknown app",
        ),
    ],
)
def test_load_manifest_rejects_project_shape_errors(
    tmp_path: Path, toml: str, message: str
) -> None:
    path = tmp_path / "hinoki.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ProjectError, match=message):
        project.load_manifest(path)


def test_load_manifest_reports_filesystem_read_error(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match=r"cannot read hinoki\.toml"):
        project.load_manifest(tmp_path / "hinoki.toml")


@pytest.mark.parametrize(
    ("app", "message"),
    [
        ([], "each app must be an"),
        ({"name": "Bad", "models": "x.y", "migrations": "x.z"}, "valid lower-case"),
        ({"name": "x", "models": "bad-name", "migrations": "x.z"}, "models must be"),
        (
            {"name": "x", "models": "x.y", "migrations": "bad-name"},
            "migrations must be",
        ),
        (
            {"name": "x", "models": "x.y", "migrations": "x.z", "requires": "core"},
            "requires must be",
        ),
        (
            {
                "name": "x",
                "models": "x.y",
                "migrations": "x.z",
                "requires": ["core", "core"],
            },
            "repeats a dependency",
        ),
    ],
)
def test_parse_app_rejects_invalid_fields(app: object, message: str) -> None:
    with pytest.raises(ProjectError, match=message):
        project._parse_app(app)
