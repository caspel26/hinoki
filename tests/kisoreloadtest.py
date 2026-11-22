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


class FakeWorker:
    """A worker process that exits only when it is killed or waited for twice."""

    def __init__(self, pid: int = 4242, *, stubborn: bool = False) -> None:
        self.pid = pid
        self.code: int | None = None
        self.stubborn = stubborn
        self.signals: list[int] = []
        self.waits = 0
        self.killed = False

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, number: int) -> None:
        self.signals.append(number)

    def kill(self) -> None:
        self.killed = True
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if timeout is not None and self.stubborn and self.waits < 2:
            raise subprocess.TimeoutExpired("worker", timeout)
        if self.code is None:
            self.code = 0
        return self.code


def rolling_supervisor(**options: Any) -> Supervisor:
    return Supervisor("app:make", socket.socket(), 1, 1.0, rolling=True, **options)


def test_a_reload_that_is_stopped_replaces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = rolling_supervisor()

    def never(_worker: int) -> None:
        raise AssertionError("no worker is started while stopping")

    monkeypatch.setattr(supervisor, "start_ready", never)
    supervisor.processes = [FakeWorker()]  # type: ignore[list-item]
    supervisor.stop.set()
    try:
        supervisor.roll()
        assert supervisor.generation == 1
    finally:
        supervisor.listener.close()


def test_a_successor_still_starting_when_stopped_drains_with_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = rolling_supervisor()
    fresh = FakeWorker()
    monkeypatch.setattr(supervisor, "spawn", lambda *_a, **_k: fresh)
    supervisor.stop.set()
    try:
        assert supervisor.start_ready(1) is None
        assert supervisor.draining == [fresh]  # type: ignore[comparison-overlap]
        assert not fresh.killed
    finally:
        supervisor.listener.close()


def test_a_successor_that_never_serves_is_killed_and_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    supervisor = rolling_supervisor()
    fresh = FakeWorker()
    monkeypatch.setattr(supervisor, "spawn", lambda *_a, **_k: fresh)
    monkeypatch.setattr(supervisor, "await_ready", lambda _fd: "exited before it")
    try:
        assert supervisor.start_ready(1) is None
        assert fresh.killed
        assert "error: the new worker exited before it" in capsys.readouterr().err
        assert supervisor.draining == []
    finally:
        supervisor.listener.close()


def test_a_replaced_worker_is_killed_once_the_grace_period_is_over() -> None:
    supervisor = rolling_supervisor()
    supervisor.grace = 0.0
    old = FakeWorker(stubborn=True)
    supervisor.worker_of[old.pid] = 7
    try:
        supervisor.retire(old)  # type: ignore[arg-type]
        assert old.signals == [signal.SIGTERM]
        assert old.killed
        assert supervisor.draining == []
        assert old.pid not in supervisor.worker_of
    finally:
        supervisor.listener.close()


def test_a_replaced_worker_is_waited_for_again_until_it_exits() -> None:
    supervisor = rolling_supervisor()
    supervisor.grace = 30.0
    old = FakeWorker(stubborn=True)
    try:
        supervisor.retire(old)  # type: ignore[arg-type]
        assert not old.killed
        assert old.waits >= 2
        assert supervisor.draining == []
    finally:
        supervisor.listener.close()


def test_a_replaced_worker_is_left_to_the_shutdown_when_stopping() -> None:
    supervisor = rolling_supervisor()
    old = FakeWorker(stubborn=True)
    supervisor.stop.set()
    try:
        supervisor.retire(old)  # type: ignore[arg-type]
        assert old.signals == [signal.SIGTERM]
        assert supervisor.draining == [old]  # type: ignore[comparison-overlap]
        assert not old.killed
    finally:
        supervisor.listener.close()


def test_a_replaced_workers_snapshot_is_removed_and_the_others_stay() -> None:
    metrics = socket.socket()
    supervisor = Supervisor("app:make", socket.socket(), 2, 1.0, metrics=metrics)
    assert supervisor.metrics_dir is not None
    directory = Path(supervisor.metrics_dir)
    try:
        for name in ("worker-3.prom", "worker-3.tmp", "worker-4.prom"):
            (directory / name).write_text("x")
        supervisor.discard_snapshot(None)
        assert len(list(directory.iterdir())) == 3
        supervisor.discard_snapshot(3)
        assert [path.name for path in directory.iterdir()] == ["worker-4.prom"]
    finally:
        supervisor.release()
        supervisor.discard_snapshots()
    plain = Supervisor("app:make", socket.socket(), 1, 1.0)
    try:
        plain.discard_snapshot(3)  # no metrics directory: nothing to remove
    finally:
        plain.release()
