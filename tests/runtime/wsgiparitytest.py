"""The same WSGI callable under Kiso and under Granian: status, headers, body,
streaming, errors and a client that leaves answer the same. Where Kiso differs
on purpose (a request body limit), the test says so."""

import contextlib
import hashlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.runtime.harness import free_port, wait_ready

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests.runtime.wsgiscenarios:app"
pytestmark = pytest.mark.runtime

# What each server adds on its own, or what differs per request.
IGNORED = {"server", "date", "connection", "transfer-encoding", "content-length"}
SERVERS = ("kiso", "granian")
Reply = tuple[int, list[tuple[str, str]], bytes]


class Running:
    def __init__(self, server: str, log: Path) -> None:
        self.server = server
        self.port = free_port()
        self.log = log
        command = [
            sys.executable,
            "-m",
            "hinoki",
            "serve",
            TARGET,
            "--interface",
            "wsgi",
            "--server",
            server,
            "--port",
            str(self.port),
        ]
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "WSGI_SCENARIO_LOG": str(log),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=20)

    def closed(self, name: str, wait: float = 5.0) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.log.exists() and f"closed {name}" in self.log.read_text():
                return True
            time.sleep(0.05)
        return False


@pytest.fixture(params=SERVERS)
def running(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Running]:
    server = Running(request.param, tmp_path / "closed.log")
    try:
        wait_ready(server.port, "/status")
        yield server
    finally:
        server.stop()


def call(
    port: int,
    method: str,
    target: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Reply:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        reply = connection.getresponse()
        data = reply.read()
        kept = sorted(
            (name.lower(), value)
            for name, value in reply.getheaders()
            if name.lower() not in IGNORED
        )
        return reply.status, kept, data
    finally:
        connection.close()


def test_status_and_repeated_headers(running: Running) -> None:
    status, headers, body = call(running.port, "GET", "/status")
    assert (status, body) == (202, b"accepted")
    assert [value for name, value in headers if name == "x-a"] == ["1", "2"]
    assert ("x-b", "3") in headers


def test_the_environ_is_the_same(running: Running) -> None:
    sent = {"X-Repeat": "one", "Content-Type": "text/plain"}
    _, _, body = call(running.port, "POST", "/environ?q=a%20b&r=1", b"abc", sent)
    text = body.decode()
    assert '"PATH_INFO": "/environ"' in text
    assert '"QUERY_STRING": "q=a%20b&r=1"' in text
    assert '"CONTENT_LENGTH": "3"' in text
    assert '"CONTENT_TYPE": "text/plain"' in text
    assert '"HTTP_X_REPEAT": "one"' in text
    assert '"wsgi.url_scheme": "http"' in text


def test_a_percent_encoded_path_reaches_the_application_decoded(
    running: Running,
) -> None:
    _, _, body = call(running.port, "GET", "/environ%2Fx")
    # PATH_INFO is decoded once, carried as latin-1 (PEP 3333).
    assert b'"PATH_INFO": "/environ/x"' in body


def test_a_large_body_under_the_limit_arrives_whole(running: Running) -> None:
    payload = os.urandom(600_000)
    _, _, body = call(running.port, "POST", "/echo", payload)
    assert body.decode() == f"{len(payload)} {hashlib.sha256(payload).hexdigest()}"


def test_an_error_before_the_response_is_a_500(running: Running) -> None:
    status, _, body = call(running.port, "GET", "/boom")
    assert status == 500
    assert b"scenario failure" not in body


def test_head_has_the_headers_and_no_body(running: Running) -> None:
    status, headers, body = call(running.port, "HEAD", "/status")
    assert (status, body) == (202, b"")
    assert ("x-b", "3") in headers


def test_the_response_streams(running: Running) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=20)
    started = time.monotonic()
    connection.request("GET", "/stream")
    reply = connection.getresponse()
    first = reply.read1(1024)
    first_at = time.monotonic() - started
    rest = reply.read()
    total = time.monotonic() - started
    connection.close()
    assert len(first) + len(rest) == 3 * 1024
    assert first_at < total - 0.3  # the head and a piece came before the end


def test_a_client_that_leaves_closes_the_iterable(running: Running) -> None:
    sock = socket.create_connection(("127.0.0.1", running.port), timeout=10)
    sock.sendall(b"GET /endless HTTP/1.1\r\nHost: x\r\n\r\n")
    assert sock.recv(256).startswith(b"HTTP/1.1 200")
    sock.close()
    assert running.closed("/endless")


def test_a_bad_content_length_is_a_client_error(running: Running) -> None:
    sock = socket.create_connection(("127.0.0.1", running.port), timeout=10)
    sock.sendall(
        b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n"
        b"Connection: close\r\n\r\n"
    )
    head = sock.recv(256)
    sock.close()
    assert head.startswith(b"HTTP/1.1 400")


def test_kiso_refuses_a_body_over_the_limit_and_granian_has_no_limit(
    running: Running,
) -> None:
    # Just over the default 1 MiB limit: Kiso decides 413 from Content-Length
    # and drains at most 1 MiB (or 2 s), so the client still reads the 413.
    status, _, _ = call(running.port, "POST", "/echo", b"x" * 1_100_000)
    expected = 413 if running.server == "kiso" else 200
    assert status == expected
