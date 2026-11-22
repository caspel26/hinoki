"""``hinoki events``: the registered handlers, limits and declared apps."""

import json
from pathlib import Path

import pytest

from hinoki import Hinoki, events
from hinoki.__main__ import main
from hinoki.events import Created, Deleted, Updated
from hinoki.orm import Model, pk
from hinoki.settings import AppSettings, EventsSettings

APP = "tests.eventsclitest:make_app"


class Note(Model, table="cli_event_notes"):
    id: int = pk()
    title: str


first = events.Handlers("notes")
second = events.Handlers()


@first.in_transaction(Created[Note])
async def audit(event: Created[Note]) -> None: ...


@first.after_commit(Updated)
def reindex(event: Updated[Note]) -> None: ...


@second.after_commit(Deleted[Note])
async def forget(event: Deleted[Note]) -> None: ...


def make_app() -> Hinoki:
    return Hinoki(
        events=[first, second],
        settings=AppSettings(events=EventsSettings(max_depth=2)),
    )


def make_bare_app() -> Hinoki:
    return Hinoki()


def test_text_lists_handlers_in_execution_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["events", APP]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[:3]] == [
        "Created[Note]",
        "Updated",
        "Deleted[Note]",
    ]
    assert "in_transaction" in lines[0]
    assert "async" in lines[0] and lines[0].endswith("tests.eventsclitest.audit")
    assert " sync " in lines[1] and "after_commit" in lines[1]
    assert " - " in lines[2]
    assert (
        "limits: events.handler_timeout=5.0, events.max_handlers=16, events.max_depth=2"
    ) in lines


def test_json_has_handlers_limits_and_apps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["events", APP, "--format", "json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["handlers"][0] == {
        "event": "Created[Note]",
        "phase": "in_transaction",
        "handler": "tests.eventsclitest.audit",
        "collection": "notes",
        "async": True,
    }
    assert [h["async"] for h in found["handlers"]] == [True, False, True]
    assert found["limits"] == {
        "handler_timeout": 5.0,
        "max_handlers": 16,
        "max_depth": 2,
    }
    assert isinstance(found["apps"], list)


def test_an_app_without_handlers_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["events", "tests.eventsclitest:make_bare_app"]) == 0
    assert "no event handlers registered" in capsys.readouterr().out


def test_an_unloadable_target_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["events", "no.such.module:app"]) == 2
    assert "error: cannot load no.such.module:app" in capsys.readouterr().err


def test_declared_apps_come_from_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "hinoki.toml").write_text(
        '[project]\napplication = "tests.eventsclitest:make_app"\n\n'
        '[[apps]]\nname = "notes"\nmodels = "legacy.notes.models"\n'
        'migrations = "legacy.notes.migrations"\n'
        'events = "legacy.notes.events:handlers"\n\n'
        '[[apps]]\nname = "plain"\nmodels = "legacy.plain.models"\n'
        'migrations = "legacy.plain.migrations"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main(["events", "--format", "json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["apps"] == [{"app": "notes", "events": "legacy.notes.events:handlers"}]

    assert main(["events"]) == 0
    assert (
        "declared in hinoki.toml: notes -> legacy.notes.events:handlers"
        in capsys.readouterr().out
    )
