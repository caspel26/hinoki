"""``hinoki app add`` registers an existing package without creating files."""

from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.project import load_manifest
from hinoki.scaffold import create


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = create("sample", tmp_path)
    monkeypatch.chdir(root)
    return root


def files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if ".venv" not in path.parts}


def test_add_registers_an_existing_package_and_derives_migrations(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = files_under(project)

    assert main(["app", "add", "billing", "--models", "legacy.billing.models"]) == 0

    assert "registered app billing" in capsys.readouterr().out
    app = next(a for a in load_manifest().apps if a.name == "billing")
    assert app.models == "legacy.billing.models"
    assert app.migrations == "legacy.billing.migrations"
    assert app.requires == ()
    # only the manifest changed: no package and no lock file left behind
    created = files_under(project) - before
    assert {path.name for path in created} == set()


def test_add_takes_migrations_dependencies_and_api_only_apps(project: Path) -> None:
    assert main(["app", "add", "core", "--models", "legacy.core.models"]) == 0
    assert (
        main(
            [
                "app",
                "add",
                "billing",
                "--models",
                "legacy.billing.models",
                "--migrations",
                "legacy.db.billing",
                "--requires",
                "core",
            ]
        )
        == 0
    )
    assert main(["app", "add", "gateway", "--requires", "core"]) == 0

    apps = {a.name: a for a in load_manifest().apps}
    billing = apps["billing"]
    assert billing.migrations == "legacy.db.billing"
    assert billing.requires == ("core",)
    gateway = apps["gateway"]
    assert not gateway.has_persistence
    assert (gateway.models, gateway.migrations, gateway.requires) == ("", "", ("core",))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["core", "--models", "legacy.models"], "already registered"),
        (["Bad-Name", "--models", "legacy.models"], "app name must be"),
        (["extra", "--models", "bad module.x"], "models must be a dotted module"),
        (["extra", "--models", "models"], "pass --migrations"),
        (["extra", "--migrations", "legacy.migrations"], "pass --models"),
        (
            ["extra", "--models", "x.models", "--requires", "ghost"],
            "requires unknown app 'ghost'",
        ),
        (
            ["extra", "--models", "x.models", "--requires", "extra"],
            "dependencies contain a cycle",
        ),
    ],
)
def test_add_rejects_invalid_registrations_and_keeps_the_manifest(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    assert main(["app", "add", "core", "--models", "legacy.core.models"]) == 0
    capsys.readouterr()
    manifest = project / "hinoki.toml"
    original = manifest.read_bytes()

    assert main(["app", "add", *arguments]) == 2

    assert message in capsys.readouterr().err
    assert manifest.read_bytes() == original
    assert not list(project.glob(".hinoki-*.toml"))


def test_add_reports_a_manifest_that_cannot_be_replaced(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("os.replace", refuse)
    original = (project / "hinoki.toml").read_bytes()

    assert main(["app", "add", "billing", "--models", "legacy.billing.models"]) == 2

    assert "could not register app 'billing'" in capsys.readouterr().err
    assert (project / "hinoki.toml").read_bytes() == original
    assert not list(project.glob(".hinoki-*.toml"))


def test_add_outside_a_project_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["app", "add", "billing", "--models", "legacy.models.x"]) == 2

    assert "hinoki.toml" in capsys.readouterr().err
