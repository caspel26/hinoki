"""The project as a whole: manifest, layering rules and the management command."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hinoki.__main__ import main

ROOT = Path(__file__).parents[1]


def test_the_manifest_lists_apps_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)

    assert main(["app", "list", "--format", "json"]) == 0

    apps = json.loads(capsys.readouterr().out)["apps"]
    assert [app["name"] for app in apps] == ["members", "tasks", "activity"]


def test_the_project_keeps_its_rules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)

    assert main(["check"]) == 0

    assert "14 rule(s): 0 violated, 0 unverifiable" in capsys.readouterr().out


def test_the_seed_command_creates_a_member_in_a_migrated_database(
    tmp_path: Path,
) -> None:
    env = {**os.environ, "TRACKER_DATABASE__URL": f"sqlite://{tmp_path / 't.sqlite3'}"}

    def run(*args: str) -> str:
        done = subprocess.run(  # noqa: S603 - fixed arguments of this interpreter
            [sys.executable, "-m", "hinoki", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    run("migrations", "apply")

    assert "synthetic-token" in run("manage", "seed", "--token", "synthetic-token")
