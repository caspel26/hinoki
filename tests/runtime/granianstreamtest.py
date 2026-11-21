"""Streamed responses under a real Granian process (ASGI)."""

import http.client
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.runtime.harness import free_port, names, spawn, wait_ready

pytestmark = pytest.mark.runtime

TARGET = "streamapp:app"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int, Path]]:
    port = free_port()
    log = tmp_path / "events.log"
    payload = tmp_path / "data.bin"
    payload.write_bytes(b"abc" * 1000)
    proc = spawn(TARGET, log, port, EXP_FILE=str(payload))
    try:
        wait_ready(port, "/ready")
        yield proc, port, log
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_pieces_arrive_before_the_body_ends(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, _ = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/pieces")
    started = time.monotonic()
    response = connection.getresponse()
    assert response.status == 200
    assert response.read(6) == b"first;"
    assert time.monotonic() - started < 0.8
    assert response.read() == b"last;"
    connection.close()


def test_a_departed_client_closes_the_source(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, log = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/endless")
    assert connection.getresponse().read(1024)
    connection.close()
    deadline = time.monotonic() + 10
    while "endless:closed" not in names(log) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert "endless:closed" in names(log)


def test_events_carry_keepalive_comments(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, _ = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert response.getheader("content-type") == "text/event-stream; charset=utf-8"
    assert response.read1(1024) == b"data: hello\n\n"
    assert response.read1(1024) == b": \n\n"
    connection.close()


def test_a_file_is_sent_whole(
    server: tuple[subprocess.Popen[bytes], int, Path],
) -> None:
    _, port, _ = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/file")
    response = connection.getresponse()
    assert response.getheader("content-length") == "3000"
    assert response.read() == b"abc" * 1000
    connection.close()
