"""Apps declare their event handlers in ``hinoki.toml``."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.events import Handlers
from hinoki.project import ProjectError, load_manifest, manifest_events
from hinoki.scaffold import create

_PROJECT_PACKAGES = {"sample", "legacy", "wrongpkg"}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = create("sample", tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.syspath_prepend(str(root))
    yield root
    for module in [m for m in sys.modules if m.split(".")[0] in _PROJECT_PACKAGES]:
        del sys.modules[module]


def test_create_never_writes_an_events_module(project: Path) -> None:
    assert main(["app", "create", "notes", "--no-input"]) == 0

    assert next(a for a in load_manifest().apps if a.name == "notes").events == ""
    assert not (project / "sample" / "apps" / "notes" / "events.py").exists()
    with pytest.raises(SystemExit):
        main(["app", "create", "other", "--events", "--no-input"])


def test_manifest_events_imports_each_apps_handlers(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["app", "create", "notes", "--no-input"]) == 0
    assert main(["app", "create", "plain", "--no-input"]) == 0
    (project / "sample" / "apps" / "notes" / "events.py").write_text(
        'from hinoki import events\n\nhandlers = events.Handlers("notes")\n',
        encoding="utf-8",
    )
    manifest = project / "hinoki.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'name = "notes"\n',
            'name = "notes"\nevents = "sample.apps.notes.events:handlers"\n',
        ),
        encoding="utf-8",
    )
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


@pytest.mark.parametrize("key", ["events"])
@pytest.mark.parametrize("bad", ["false", "0"])
def test_a_non_string_target_is_rejected_even_when_falsy(
    project: Path, key: str, bad: str
) -> None:
    manifest = project / "hinoki.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[apps]]\nname = "a"\nmodels = "sample.models"\n'
        + f'migrations = "sample.migrations"\n{key} = {bad}\n',
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match=f"{key} must be a module:attribute"):
        load_manifest()
