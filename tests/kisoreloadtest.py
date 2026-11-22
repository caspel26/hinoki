"""Reload on SIGHUP: ``hinoki serve`` replaces its workers one at a time, in real
processes on loopback sockets."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hinoki.runtime.listener import signal_ready
from hinoki.runtime.supervisor import Supervisor

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGHUP"), reason="Windows has no SIGHUP"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until(condition: Callable[[], object], seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except OSError:
            pass
        time.sleep(0.05)
    pytest.fail("timed out")


@dataclass
class Served:
    process: subprocess.Popen[bytes]
    port: int
    metrics_port: int
    state: Path
    log: Path

    def fetch(self, target: str) -> Any:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{target}", timeout=30
        ) as answer:
            return json.load(answer)

    def pid(self) -> int:
        return int(self.fetch("/pid")["pid"])

    def scrape(self) -> str:
        url = f"http://127.0.0.1:{self.metrics_port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as answer:
            return str(answer.read().decode())

    def logged(self, text: str) -> bool:
        return text in self.log.read_text()

    def hangup(self) -> None:
        self.process.send_signal(signal.SIGHUP)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    port, metrics_port = free_port(), free_port()
    log = tmp_path / "output.log"
    environment = {
        **os.environ,
        "RELOAD_STATE": str(tmp_path),
        "METRICS_PORT": str(metrics_port),
    }
    with log.open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hinoki",
                "serve",
                "tests.kisoreloadapp:create_app",
                "--server",
                "kiso",
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    running = Served(process, port, metrics_port, tmp_path, log)
    try:
        wait_until(running.pid)
        yield running
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_reload_serves_through_it_and_lets_a_running_request_finish(
    served: Served,
) -> None:
    old = served.pid()
    outcome: dict[str, Any] = {}

    def hold() -> None:
        outcome["answer"] = served.fetch("/hold")

    holding = threading.Thread(target=hold)
    holding.start()
    wait_until((served.state / f"held-{old}").exists)
    failures: list[Exception] = []
    probing = threading.Event()

    def probe() -> None:
        while not probing.is_set():
            try:
                served.pid()
            except Exception as error:
                failures.append(error)

    prober = threading.Thread(target=probe)
    prober.start()
    try:
        served.hangup()
        wait_until(lambda: served.pid() != old)
        # The old worker has stopped accepting but still owes its answer.
        assert alive(old)
        os.kill(old, signal.SIGUSR1)
        holding.join(timeout=30)
        wait_until(lambda: served.logged("reloaded 1 workers (generation 2)"))
    finally:
        probing.set()
        prober.join(timeout=30)
    assert outcome["answer"] == {"pid": old}
    assert failures == [], failures
    wait_until(lambda: not alive(old))
    page = served.scrape()
    assert 'worker="1"' in page
    assert 'worker="0"' not in page


def test_a_new_worker_that_does_not_start_leaves_the_old_one_serving(
    served: Served,
) -> None:
    old = served.pid()
    (served.state / "broken").touch()
    served.hangup()
    wait_until(lambda: served.logged("error: reload stopped at worker 1 of 1"))
    assert served.process.poll() is None
    assert served.pid() == old
    (served.state / "broken").unlink()
    served.hangup()
    wait_until(lambda: served.pid() != old)
    wait_until(lambda: served.logged("reloaded 1 workers (generation 2)"))
    assert not alive(old)


def test_sighup_stops_the_server_when_the_setting_says_stop(tmp_path: Path) -> None:
    port, metrics_port = free_port(), free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            "tests.kisoreloadapp:create_app",
            "--server",
            "kiso",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "RELOAD_STATE": str(tmp_path),
            "METRICS_PORT": str(metrics_port),
            "RELOAD_SIGHUP": "stop",
        },
    )
    try:

        def answers() -> bool:
            url = f"http://127.0.0.1:{port}/pid"
            with urllib.request.urlopen(url, timeout=5):
                return True

        wait_until(answers)
        process.send_signal(signal.SIGHUP)
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def readiness(ready_wait: float) -> tuple[Supervisor, int, int]:
    supervisor = Supervisor("app:make", socket.socket(), 1, 1.0, startup_timeout=0)
    supervisor.ready_wait = ready_wait
    reader, writer = os.pipe()
    return supervisor, reader, writer


def test_a_worker_is_ready_when_its_byte_arrives() -> None:
    supervisor, reader, writer = readiness(30.0)
    try:
        signal_ready(writer)
        assert supervisor.await_ready(reader) is None
    finally:
        os.close(reader)
        supervisor.listener.close()


def test_a_worker_that_closes_the_pipe_without_the_byte_failed() -> None:
    supervisor, reader, writer = readiness(30.0)
    try:
        os.close(writer)
        reason = supervisor.await_ready(reader)
        assert reason == "exited before it was ready"
    finally:
        os.close(reader)
        supervisor.listener.close()


def test_a_worker_that_says_nothing_is_given_up_on_after_the_wait() -> None:
    supervisor, reader, writer = readiness(0.2)
    try:
        reason = supervisor.await_ready(reader)
        assert reason is not None
        assert reason.startswith("was not ready after")
    finally:
        os.close(reader)
        os.close(writer)
        supervisor.listener.close()


@pytest.mark.parametrize(
    ("rolling", "reload", "sets"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_sighup_only_requests_a_reload_when_the_supervisor_rolls(
    rolling: bool, reload: bool, sets: bool
) -> None:
    supervisor = Supervisor(
        "app:make", socket.socket(), 1, 1.0, reload=reload, rolling=rolling
    )
    try:
        supervisor.on_signal(signal.SIGHUP, None)
        assert supervisor.hangup.is_set() is sets
        assert supervisor.stop.is_set() is not sets
    finally:
        supervisor.listener.close()


def test_sighup_while_stopping_never_counts_toward_the_kill() -> None:
    supervisor = Supervisor("app:make", socket.socket(), 1, 1.0, rolling=True)
    try:
        supervisor.on_signal(signal.SIGTERM, None)
        supervisor.on_signal(signal.SIGHUP, None)
        assert supervisor.signals == 1
        assert not supervisor.hangup.is_set()
    finally:
        supervisor.listener.close()


def test_a_worker_that_cannot_be_spawned_does_not_leak_its_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = Supervisor("app:make", socket.socket(), 1, 1.0, rolling=True)
    opened: list[int] = []
    real_pipe = os.pipe

    def tracked_pipe() -> tuple[int, int]:
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def refuse(slot: int = 0, ready_fd: int | None = None) -> Any:
        raise OSError("no more processes")

    monkeypatch.setattr(os, "pipe", tracked_pipe)
    monkeypatch.setattr(supervisor, "spawn", refuse)
    try:
        assert supervisor.start_ready(1) is None
        for descriptor in opened:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptor)
    finally:
        supervisor.listener.close()
