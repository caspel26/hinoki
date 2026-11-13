"""``python -m hinoki serve`` runs an application from its server settings."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from hinoki import FrameworkSettings, Hinoki
from hinoki.serve import load_target, server_settings
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
    assert isinstance(FrameworkSettings.load().server.port, int)
    assert Hinoki().settings is None


def test_serve_starts_the_application_on_the_configured_port() -> None:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "hinoki", "serve", "tests.runtime.serveapp:create_app"],
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
        process.wait(timeout=20)
