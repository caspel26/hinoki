"""Characterisation of Granian's ASGI lifespan behaviour (see RUNTIME-DESIGN §1.1)."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.runtime

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def spawn(
    tmp_path: Path, mode: str, workers: int, port: int
) -> subprocess.Popen[bytes]:
    env = {**os.environ, "EXP_LOG": str(tmp_path / "events.log"), "EXP_MODE": mode}
    cmd = [
        sys.executable, "-m", "granian", "--interface", "asgi",
        "--workers", str(workers), "--port", str(port),
        "--no-log", "lifespanapp:app",
    ]  # fmt: skip
    return subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.DEVNULL)


def events(tmp_path: Path) -> list[tuple[str, str]]:
    log = tmp_path / "events.log"
    if not log.exists():
        return []
    return [tuple(line.split()[1:3]) for line in log.read_text().splitlines()]  # type: ignore[misc]


def fetch(port: int, path: str = "/") -> str:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return str(resp.read().decode())


def wait_ready(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            fetch(port)
        except OSError:
            time.sleep(0.1)
        else:
            return
    raise AssertionError("server did not become ready")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    port = free_port()
    proc = spawn(tmp_path, "ok", 2, port)
    try:
        wait_ready(port)
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_failed_startup_exits_nonzero_and_never_serves(tmp_path: Path) -> None:
    proc = spawn(tmp_path, "fail", 1, free_port())
    assert proc.wait(timeout=20) != 0
    assert events(tmp_path)[0][1] == "startup-failed"


def test_each_worker_runs_its_own_lifespan(
    tmp_path: Path, server: tuple[subprocess.Popen[bytes], int]
) -> None:
    _, port = server
    pids = {fetch(port) for _ in range(40)}
    started = {pid for pid, event in events(tmp_path) if event == "startup"}
    assert len(started) == 2
    assert pids <= started


def test_sigterm_lets_inflight_request_finish_before_shutdown(
    tmp_path: Path, server: tuple[subprocess.Popen[bytes], int]
) -> None:
    proc, port = server
    result: list[str] = []
    client = threading.Thread(target=lambda: result.append(fetch(port, "/slow")))
    client.start()
    deadline = time.monotonic() + 5
    while not any(e == "slow-start" for _, e in events(tmp_path)):
        assert time.monotonic() < deadline
        time.sleep(0.05)

    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
    client.join(timeout=5)

    assert len(result) == 1, "in-flight request must complete"
    busy_pid = result[0]
    order = [e for pid, e in events(tmp_path) if pid == busy_pid]
    assert order.index("slow-end") < order.index("shutdown")
