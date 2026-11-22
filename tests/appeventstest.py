"""Apps declare their event handlers in ``hinoki.toml``."""

import sys
from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.events import Handlers
from hinoki.project import ProjectError, load_manifest, manifest_events
from hinoki.scaffold import create


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = create("sample", tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.syspath_prepend(str(root))
    return root


def test_create_with_events_writes_the_module_and_the_manifest_key(
    project: Path,
) -> None:
    assert main(["app", "create", "notes", "--events", "--no-input"]) == 0

    app = next(a for a in load_manifest().apps if a.name == "notes")
    assert app.events == "sample.apps.notes.events:handlers"
    assert (project / "sample" / "apps" / "notes" / "events.py").is_file()


def test_create_without_events_leaves_the_key_out(project: Path) -> None:
    assert main(["app", "create", "notes", "--no-input"]) == 0

    assert next(a for a in load_manifest().apps if a.name == "notes").events == ""
    assert not (project / "sample" / "apps" / "notes" / "events.py").exists()


def test_manifest_events_imports_each_apps_handlers(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["app", "create", "notes", "--events", "--no-input"]) == 0
    assert main(["app", "create", "plain", "--no-input"]) == 0
    for name in [m for m in sys.modules if m.startswith("sample")]:
        monkeypatch.delitem(sys.modules, name)

    found = manifest_events(project)

    assert len(found) == 1
    assert isinstance(found[0], Handlers)


def test_add_and_list_show_the_events_target(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "app",
                "add",
                "billing",
                "--models",
                "legacy.billing.models",
                "--events",
                "legacy.billing.events:handlers",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["app", "list"]) == 0

    assert "events: legacy.billing.events:handlers" in capsys.readouterr().out


def test_an_unresolvable_or_wrong_events_target_is_an_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["app", "add", "a", "--models", "nope.models", "--events", "nope.events:h"])
    with pytest.raises(ProjectError, match="cannot load the events of app 'a'"):
        manifest_events(project)

    package = project / "wrongpkg"
    package.mkdir()
    (package / "__init__.py").write_text("handlers = 1\n", encoding="utf-8")
    main(
        [
            "app",
            "add",
            "b",
            "--models",
            "wrongpkg.models",
            "--events",
            "wrongpkg:handlers",
        ]
    )
    manifest = load_manifest()
    with pytest.raises(ProjectError):
        manifest.event_handlers()
