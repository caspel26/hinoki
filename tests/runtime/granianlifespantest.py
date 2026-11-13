"""Characterisation of Granian's ASGI lifespan behaviour (see RUNTIME-DESIGN §1.1)."""

import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.runtime.harness import events, fetch, free_port, names, spawn, wait_ready

pytestmark = pytest.mark.runtime


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    port = free_port()
    proc = spawn("lifespanapp:app", tmp_path / "events.log", port, workers=2)
    try:
        wait_ready(port)
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_failed_startup_exits_nonzero_and_never_serves(tmp_path: Path) -> None:
    log = tmp_path / "events.log"
    proc = spawn("lifespanapp:app", log, free_port(), EXP_MODE="fail")
    assert proc.wait(timeout=20) != 0
    assert names(log)[0] == "startup-failed"


def test_each_worker_runs_its_own_lifespan(
    tmp_path: Path, server: tuple[subprocess.Popen[bytes], int]
) -> None:
    _, port = server
    pids = {fetch(port) for _ in range(40)}
    started = {
        pid for pid, event in events(tmp_path / "events.log") if event == "startup"
    }
    assert len(started) == 2
    assert pids <= started


def test_sigterm_lets_inflight_request_finish_before_shutdown(
    tmp_path: Path, server: tuple[subprocess.Popen[bytes], int]
) -> None:
    proc, port = server
    log = tmp_path / "events.log"
    result: list[str] = []
    client = threading.Thread(target=lambda: result.append(fetch(port, "/slow")))
    client.start()
    deadline = time.monotonic() + 5
    while "slow-start" not in names(log):
        assert time.monotonic() < deadline
        time.sleep(0.05)

    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
    client.join(timeout=5)

    assert len(result) == 1, "in-flight request must complete"
    order = [event for pid, event in events(log) if pid == result[0]]
    assert order.index("slow-end") < order.index("shutdown")
