"""``hinoki serve|dev --server rust`` as real processes: one worker, several
workers under a supervisor, graceful stop, a crashed worker and dev reload."""

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

from hinoki.runtime.recent import read_recent
from tests.runtime.harness import fetch, free_port, wait_ready

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests.kisoserverapp:app"
pytestmark = pytest.mark.runtime


def start(
    command: str, target: str, port: int, *flags: str, cwd: Path = ROOT
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "hinoki", command, target, "--port", str(port), *flags],
        cwd=cwd,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def children(pid: int) -> list[int]:
    command = ["pgrep", "-P", str(pid)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    return [int(line) for line in done.stdout.split()]


def is_running(pid: int) -> bool:
    """Whether the process exists and is not a zombie (an orphan reparented to
    an init that does not reap stays one)."""
    command = ["ps", "-o", "stat=", "-p", str(pid)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    state = done.stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_until(condition: object, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def served() -> Iterator[list[subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=20)
        if process.stdout:
            process.stdout.close()


def slow_request(port: int, result: list[str]) -> threading.Thread:
    def call() -> None:
        try:
            result.append(fetch(port, "/slow"))
        except (OSError, urllib.error.URLError) as error:
            result.append(f"failed: {error}")

    thread = threading.Thread(target=call)
    thread.start()
    return thread


@pytest.mark.parametrize("workers", [1, 2])
def test_serve_runs_workers_and_stops_gracefully_with_a_request_in_flight(
    workers: int, served: list[subprocess.Popen[str]]
) -> None:
    port = free_port()
    server = start("serve", TARGET, port, "--server", "kiso", "--workers", str(workers))
    served.append(server)
    wait_ready(port, "/health")
    # A supervisor and one process per worker, also for one worker (SIGHUP reload).
    assert len(children(server.pid)) == workers
    assert '"ok"' in fetch(port, "/health")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="HEAD")
    with urllib.request.urlopen(request, timeout=10) as head:
        assert head.status == 200
        assert head.read() == b""
    result: list[str] = []
    thread = slow_request(port, result)
    time.sleep(0.15)
    workers_alive = children(server.pid)
    server.send_signal(signal.SIGTERM)
    thread.join(timeout=10)
    assert result == ['{"slow":"done"}']
    assert server.wait(timeout=20) == 0
    assert not any(is_running(pid) for pid in workers_alive)
    with pytest.raises(OSError, match="refused"):
        fetch(port, "/health")


def test_a_worker_that_dies_stops_the_others_and_the_supervisor_fails(
    served: list[subprocess.Popen[str]],
) -> None:
    port = free_port()
    server = start("serve", TARGET, port, "--server", "kiso", "--workers", "2")
    served.append(server)
    wait_ready(port, "/health")
    workers = children(server.pid)
    assert len(workers) == 2
    os.kill(workers[0], signal.SIGKILL)
    assert server.wait(timeout=30) != 0
    assert wait_until(lambda: not any(is_running(pid) for pid in workers))
    assert server.stdout is not None
    assert "a worker exited" in server.stdout.read()
    with pytest.raises(OSError, match="refused"):
        fetch(port, "/health")


def test_workers_stop_when_the_supervisor_is_killed(
    served: list[subprocess.Popen[str]],
) -> None:
    port = free_port()
    server = start("serve", TARGET, port, "--server", "kiso", "--workers", "2")
    served.append(server)
    wait_ready(port, "/health")
    workers = children(server.pid)
    os.kill(server.pid, signal.SIGKILL)
    assert wait_until(lambda: not any(is_running(pid) for pid in workers), 20)


def run_logged(command: str, env: dict[str, str], *flags: str) -> str:
    """Serve ``serveapp`` with ``env``, request /ping and an unknown path, stop it
    and return everything it printed."""
    port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki",
            command,
            "tests.runtime.serveapp:create_app",
            "--port",
            str(port),
            *flags,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        wait_ready(port, "/ping")
        with contextlib.suppress(urllib.error.HTTPError):
            fetch(port, "/missing?x=1")
        # The log thread is not joined at exit: let it hand over its batch first.
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        assert process.stdout is not None
        return process.stdout.read()
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=20)
        if process.stdout:
            process.stdout.close()


@pytest.mark.parametrize(
    ("command", "env", "present"),
    [
        ("serve", {}, False),
        ("serve", {"SRV_SERVER__SERVER_TIMING": "true"}, True),
        ("dev", {}, True),
        ("dev", {"SRV_SERVER__SERVER_TIMING": "false"}, False),
    ],
)
def test_server_timing_is_on_in_dev_and_when_asked(
    command: str,
    env: dict[str, str],
    present: bool,
    monkeypatch: pytest.MonkeyPatch,
    served: list[subprocess.Popen[str]],
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    port = free_port()
    server = start(
        command, "tests.runtime.serveapp:create_app", port, "--server", "kiso"
    )
    served.append(server)
    wait_ready(port, "/ping")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=10) as reply:
        timing = reply.headers.get("server-timing")
    assert (timing is not None) is present
    if timing is not None:
        assert re.fullmatch(r'kiso;dur=\d+\.\d{3};desc="(python|native)"', timing)


def test_serve_logs_requests_only_when_the_setting_is_on() -> None:
    quiet = run_logged("serve", {}, "--workers", "1")
    assert "HTTP/1.1" not in quiet
    loud = run_logged("serve", {"SRV_SERVER__ACCESS_LOG": "true"}, "--workers", "1")
    assert '"GET /ping HTTP/1.1" 200 OK' in loud
    assert '"GET /missing?x=1 HTTP/1.1" 404 Not Found' in loud
    assert "\x1b[" not in loud  # a pipe is not a terminal: no colour
    assert "Kiso " in loud and "serve, 1 worker, http, access log on" in loud


def test_the_json_format_writes_one_object_per_request() -> None:
    output = run_logged(
        "serve", {"SRV_SERVER__ACCESS_LOG_FORMAT": "json"}, "--workers", "1"
    )
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    missing = next(item for item in records if item["path"] == "/missing")
    assert missing["method"] == "GET" and missing["query"] == "x=1"
    assert missing["status"] == 404 and missing["duration_ms"] >= 0
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", missing["time"])
    assert missing["lane"] == "early"
    ping = next(item for item in records if item["path"] == "/ping")
    assert ping["lane"] in {"python", "native"}
    assert ping["route"] == "/ping" and "route" not in missing
    assert "\x1b[" not in output


def test_the_logger_format_routes_access_records_through_logging() -> None:
    output = run_logged(
        "serve", {"SRV_SERVER__ACCESS_LOG_FORMAT": "logger"}, "--workers", "1"
    )
    line = next(item for item in output.splitlines() if " GET /missing " in item)
    assert "INFO hinoki.access: 127.0.0.1:" in line and " 404 " in line
    assert "HTTP/1.1" not in output


def test_recent_requests_are_recorded_without_printing_an_access_log(
    tmp_path: Path,
) -> None:
    file = tmp_path / "requests.jsonl"
    output = run_logged(
        "serve", {"SRV_SERVER__RECENT_REQUESTS": str(file)}, "--workers", "1"
    )
    assert "GET /missing" not in output
    found = {item["path"]: item for item in read_recent(file, 10)}
    assert found["/missing"]["status"] == 404 and found["/ping"]["status"] == 200


def test_skipped_paths_and_a_slow_floor_leave_successes_out_not_failures() -> None:
    output = run_logged(
        "serve",
        {
            "SRV_SERVER__ACCESS_LOG_FORMAT": "plain",
            "SRV_SERVER__ACCESS_LOG_SKIP": "/ping",
        },
        "--workers",
        "1",
    )
    assert "GET /missing?x=1" in output and "GET /ping" not in output
    quiet = run_logged(
        "serve",
        {
            "SRV_SERVER__ACCESS_LOG_FORMAT": "plain",
            "SRV_SERVER__ACCESS_LOG_SLOW_MS": "60000",
        },
        "--workers",
        "1",
    )
    assert "GET /missing?x=1" in quiet and "GET /ping" not in quiet


def test_dev_logs_requests_unless_the_setting_is_off() -> None:
    dev = run_logged("dev", {})
    assert '"GET /ping HTTP/1.1" 200 OK' in dev
    assert "dev, reloads on file changes, 1 worker" in dev
    off = run_logged("dev", {"SRV_SERVER__ACCESS_LOG": "false"})
    assert "HTTP/1.1" not in off


def test_the_two_servers_are_refused_their_foreign_settings(
    served: list[subprocess.Popen[str]],
) -> None:
    for env, flags, text in (
        (
            {"SRV_SERVER__MAX_CONNECTIONS": "9"},
            ["--server", "granian"],
            "max_connections",
        ),
    ):
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "hinoki",
                "serve",
                "tests.runtime.serveapp:create_app",
                *flags,
            ],
            cwd=ROOT,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 2
        assert text in done.stderr


APP = """
from hinoki import Hinoki, http

@http.get("/v")
async def v() -> dict[str, str]:
    return {{"v": "{version}"}}

app = Hinoki()
app.include(v)
"""


def read_until(stream: IO[str], text: str, seconds: float = 30.0) -> str:
    seen: list[str] = []
    found = threading.Event()

    def pump() -> None:
        for line in stream:
            seen.append(line)
            if text in line:
                found.set()
                return

    threading.Thread(target=pump, daemon=True).start()
    assert found.wait(seconds), "".join(seen)
    return "".join(seen)


def test_dev_reloads_on_a_change_and_waits_after_an_error(
    tmp_path: Path, served: list[subprocess.Popen[str]]
) -> None:
    module = tmp_path / "devproject.py"
    module.write_text(APP.format(version="one"))
    port = free_port()
    server = start("dev", "devproject:app", port, "--server", "kiso", cwd=tmp_path)
    served.append(server)
    assert server.stdout is not None
    wait_ready(port, "/v")
    assert '"one"' in fetch(port, "/v")
    # A different size each time: the same-size edit within one second would
    # keep the old bytecode (its timestamp and size are unchanged).
    module.write_text(APP.format(version="second"))
    assert wait_until(lambda: '"second"' in _try(port))
    module.write_text("def broken(\n")
    read_until(server.stdout, "waiting for changes")
    # The supervisor still owns the socket: a client connects and waits.
    with socket.create_connection(("127.0.0.1", port), timeout=2) as waiting:
        waiting.sendall(b"GET /v HTTP/1.1\r\nhost: t\r\n\r\n")
        waiting.settimeout(1)
        with pytest.raises(TimeoutError):
            waiting.recv(1)
    module.write_text(APP.format(version="the third"))
    assert wait_until(lambda: '"the third"' in _try(port))
    server.send_signal(signal.SIGINT)
    assert server.wait(timeout=30) == 0


def _try(port: int) -> str:
    try:
        return fetch(port, "/v")
    except OSError:
        return ""


def spawn_pids(server: subprocess.Popen[str], count: int) -> list[int]:
    assert wait_until(lambda: len(children(server.pid)) == count)
    return children(server.pid)


def test_workers_stop_when_the_supervisor_dies_while_they_import(
    served: list[subprocess.Popen[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLOW_IMPORT", "3")
    port = free_port()
    server = start(
        "serve", "tests.runtime.slowimportapp:app", port, "--server", "kiso",
        "--workers", "2",
    )  # fmt: skip
    served.append(server)
    workers = spawn_pids(server, 2)
    time.sleep(1.5)  # past the parent check in main, inside the 3 s import
    os.kill(server.pid, signal.SIGKILL)
    assert wait_until(lambda: not any(is_running(pid) for pid in workers), 30)


def test_a_child_of_a_handler_does_not_hold_the_listening_socket(
    served: list[subprocess.Popen[str]],
) -> None:
    port = free_port()
    server = start(
        "serve",
        "tests.runtime.spawnapp:app",
        port,
        "--server",
        "kiso",
        "--workers",
        "2",
    )
    served.append(server)
    wait_ready(port, "/health")
    sleeper = int(fetch(port, "/spawn").split(":")[1].strip("}"))
    try:
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=20) == 0
        assert is_running(sleeper)  # the child outlives the server
        with socket.socket() as probe:  # the port is free again
            # Reuse only waives TIME_WAIT; a listening socket still blocks it.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    finally:
        os.kill(sleeper, signal.SIGKILL)


def test_a_client_arriving_during_a_graceful_stop_is_refused(
    served: list[subprocess.Popen[str]],
) -> None:
    port = free_port()
    server = start("serve", TARGET, port, "--server", "kiso", "--workers", "2")
    served.append(server)
    wait_ready(port, "/health")
    result: list[str] = []
    thread = slow_request(port, result)
    time.sleep(0.1)
    server.send_signal(signal.SIGTERM)
    time.sleep(0.15)  # the workers are draining the request still running
    with pytest.raises(OSError, match="refused"):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    thread.join(timeout=10)
    assert result == ['{"slow":"done"}']
    assert server.wait(timeout=20) == 0


@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize("fault", ["busy", "port70000", "port-1", "port65536", "host"])
def test_a_listen_failure_is_one_clean_error_whatever_the_workers(
    workers: str, fault: str
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        flags = {
            "busy": ["--port", str(taken.getsockname()[1])],
            "port70000": ["--port", "70000"],
            "port-1": ["--port", "-1"],
            "port65536": ["--port", "65536"],
            "host": ["--host", "no-such-host.invalid"],
        }[fault]
        done = subprocess.run(
            [sys.executable, "-m", "hinoki", "serve", TARGET, "--server", "kiso",
             "--workers", workers, *flags],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert done.returncode == 2
    assert "error: cannot listen on" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("workers", ["-1", "257"])
def test_workers_outside_the_range_are_refused(workers: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "hinoki", "serve", TARGET, "--server", "kiso",
         "--workers", workers],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == 2
    assert "--workers must be 1 to 256" in done.stderr
