"""Hinoki's own adapter and lifespan under a real Granian process."""

import json
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.runtime.harness import events, fetch, free_port, names, spawn, wait_ready

pytestmark = pytest.mark.runtime

TARGET = "hinokiapp:app"
STARTED = ["start:alpha", "start:beta", "start:gamma"]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int, Path]]:
    port = free_port()
    log = tmp_path / "events.log"
    proc = spawn(TARGET, log, port)
    try:
        wait_ready(port, "/fast")
        yield proc, port, log
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_module_failure_aborts_startup_and_unwinds_in_reverse(tmp_path: Path) -> None:
    log = tmp_path / "events.log"
    proc = spawn(TARGET, log, free_port(), EXP_FAIL="gamma")
    assert proc.wait(timeout=20) != 0
    assert names(log) == [*STARTED, "stop:beta", "stop:alpha"]


def test_sigterm_stops_modules_in_reverse_dependency_order(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    proc, _, log = server
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
    assert names(log) == [*STARTED, "stop:gamma", "stop:beta", "stop:alpha"]


def test_sync_handler_does_not_block_async_requests(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, _ = server
    slow = threading.Thread(target=fetch, args=(port, "/sync-slow"))
    slow.start()
    time.sleep(0.2)
    start = time.monotonic()
    for _ in range(10):
        fetch(port, "/fast")
    elapsed = time.monotonic() - start
    slow.join(timeout=5)
    assert elapsed < 0.7, (
        f"async requests were delayed by the sync handler ({elapsed:.2f}s)"
    )


def test_two_workers_run_separate_lifespans(tmp_path: Path) -> None:
    port, log = free_port(), tmp_path / "events.log"
    proc = spawn(TARGET, log, port, workers=2)
    try:
        wait_ready(port, "/fast")
        pids = {fetch(port, "/fast") for _ in range(40)}
        starters = {pid for pid, event in events(log) if event == "start:gamma"}
        assert len(starters) == 2
        assert len(pids) == 2
    finally:
        proc.kill()
        proc.wait()


def test_invalid_handler_signature_stops_the_server_instead_of_serving(
    tmp_path: Path,
) -> None:
    proc = spawn("hinokibadapp:app", tmp_path / "events.log", free_port())
    assert proc.wait(timeout=20) != 0


def test_encoded_slash_stays_inside_one_path_segment(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, _ = server
    assert json.loads(fetch(port, "/echo/a%2Fb")) == {"name": "a/b"}
    assert json.loads(fetch(port, "/echo/%E4%BD%A0")) == {"name": "\u4f60"}
