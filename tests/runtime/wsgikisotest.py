"""``hinoki serve --interface wsgi`` on Kiso as real processes: the admission
bound, a graceful stop with a request in flight, several workers and the
refusals."""

import contextlib
import http.client
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.runtime.harness import free_port, wait_ready

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = "tests.runtime.wsgiscenarios:app"
LIMITS = "tests.runtime.wsgilimitsapp:wsgi_app"
pytestmark = pytest.mark.runtime


def start(target: str, port: int, *flags: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            target,
            "--interface",
            "wsgi",
            "--port",
            str(port),
            *flags,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def finish(process: subprocess.Popen[str]) -> str:
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGTERM)
    try:
        output, _ = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
    return output


@pytest.fixture
def served() -> Iterator[tuple[subprocess.Popen[str], int]]:
    port = free_port()
    process = start(SCENARIOS, port)
    try:
        wait_ready(port, "/status")
        yield process, port
    finally:
        finish(process)


def status_of(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path)
        reply = connection.getresponse()
        headers = {name.lower(): value for name, value in reply.getheaders()}
        return reply.status, headers, reply.read()
    finally:
        connection.close()


def test_a_full_pool_and_queue_answer_503_with_retry_after() -> None:
    port = free_port()
    process = start(LIMITS, port)
    try:
        wait_ready(port, "/ping")
        found: list[tuple[int, dict[str, str], bytes]] = []
        threads = [
            threading.Thread(target=lambda: found.append(status_of(port, "/slow")))
            for _ in range(5)
        ]
        for thread in threads:
            thread.start()
            time.sleep(0.05)
        for thread in threads:
            thread.join(timeout=30)
    finally:
        finish(process)
    busy = [reply for reply in found if b"the server is busy" in reply[2]]
    assert busy, [reply[0] for reply in found]
    assert all(reply[0] == 503 and reply[1]["retry-after"] == "1" for reply in busy)
    assert any(reply[0] == 200 for reply in found)


def test_a_graceful_stop_lets_a_streaming_response_finish(
    served: tuple[subprocess.Popen[str], int],
) -> None:
    process, port = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", "/stream")
    reply = connection.getresponse()
    assert len(reply.read1(1024)) > 0
    process.send_signal(signal.SIGTERM)
    rest = reply.read()
    connection.close()
    assert len(rest) == 2 * 1024  # the two pieces after the first
    assert process.wait(timeout=30) == 0
    with pytest.raises(OSError, match="refused"):
        status_of(port, "/status")


def test_several_workers_serve_under_a_supervisor() -> None:
    port = free_port()
    process = start(SCENARIOS, port, "--workers", "2")
    try:
        wait_ready(port, "/status")
        replies = {status_of(port, "/status")[0] for _ in range(10)}
    finally:
        output = finish(process)
    assert replies == {202}
    assert process.returncode == 0, output


def test_a_hinoki_application_is_refused_as_a_wsgi_target() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            "tests.kisoserverapp:app",
            "--interface",
            "wsgi",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode != 0
    assert "WSGIAdapter" in done.stderr
