"""``python -m hinoki serve`` runs an application from its server settings."""

import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hinoki import Hinoki
from hinoki.__main__ import main
from hinoki.serve import load_target, server_settings
from hinoki.settings import AppSettings
from tests.runtime.harness import fetch, free_port, wait_ready

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.runtime


def test_targets_are_applications_or_factories() -> None:
    app, factory = load_target("tests.runtime.serveapp:create_app")
    assert factory is True and callable(app)
    for bad in (
        "nope",
        "tests.runtime.serveapp:missing",
        "tests.runtime.serveapp:ping",
    ):
        with pytest.raises(SystemExit):
            load_target(bad)


def test_the_server_group_comes_from_the_applications_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SRV_SERVER__PORT", "9123")
    monkeypatch.setenv("SRV_SERVER__WORKERS", "2")
    settings = server_settings("tests.runtime.serveapp:create_app")
    assert (settings.port, settings.workers, settings.host) == (9123, 2, "127.0.0.1")
    monkeypatch.setenv("SRV_SERVER__PORT", "0")
    with pytest.raises(SystemExit, match="port must be"):
        server_settings("tests.runtime.serveapp:create_app")
    assert isinstance(AppSettings.load().server.port, int)
    assert Hinoki().settings is None


@pytest.mark.parametrize("command", ["serve", "dev"])
def test_serve_and_dev_start_the_application_on_the_configured_port(
    command: str,
) -> None:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "hinoki", command, "tests.runtime.serveapp:create_app"],
        cwd=ROOT,
        env={**os.environ, "SRV_SERVER__PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_ready(port, "/ping")
        assert '"pong"' in fetch(port, "/ping")
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=20)


def test_dev_explains_a_missing_reload_dependency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = importlib.util.find_spec

    def without_watchfiles(name: str, package: str | None = None) -> object:
        return None if name == "watchfiles" else real(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", without_watchfiles)
    assert main(["dev", "tests.runtime.serveapp:create_app"]) == 2
    error = capsys.readouterr().err
    assert "watchfiles" in error and "hinoki[server]" in error
