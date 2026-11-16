"""The Rust HTTP/1.1 server over real loopback sockets."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, RuntimeConfig, http
from hinoki.modules import ModuleContext
from hinoki.request import Request
from hinoki.runtime import rustserver
from hinoki.runtime.rustserver import RustWorker
from hinoki.settings import AppSettings, ServerSettings
from tests.harness import Client

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Probe:
    """What the handlers share with the test that drives them."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    entered: int = 0
    cancelled: int = 0
    shutdown_ran: bool = False


PROBE = Probe()


@pytest.fixture(autouse=True)
def fresh_probe() -> None:
    global PROBE  # noqa: PLW0603 - handlers are module level and read it
    PROBE = Probe()


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


@http.get("/block")
async def block() -> dict[str, str]:
    PROBE.entered += 1
    try:
        await PROBE.gate.wait()
    except asyncio.CancelledError:
        PROBE.cancelled += 1
        raise
    return {"released": "yes"}


async def note_shutdown(ctx: ModuleContext) -> None:
    PROBE.shutdown_ran = True


async def fail_startup(ctx: ModuleContext) -> None:
    raise RuntimeError("no database")


def make_app(**config: Any) -> Hinoki:
    return Hinoki(
        include=[health, made, block],
        runtime=RuntimeConfig(**config),
        on_shutdown=[note_shutdown],
    )


@pytest.fixture
async def served() -> AsyncIterator[tuple[RustWorker, int]]:
    worker = RustWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    yield worker, port
    await worker.stop()


@dataclass
class Received:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str:
        return next(v for k, v in self.headers if k == name)

    def json(self) -> Any:
        return json.loads(self.body)


async def read_response(
    reader: asyncio.StreamReader, head_only: bool = False
) -> Received:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next((int(v) for k, v in headers if k == "content-length"), 0)
    body = b"" if head_only else await asyncio.wait_for(reader.readexactly(length), 5)
    return Received(status, headers, body)  # type: ignore[arg-type]


async def open_connection(
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def fetch(port: int, method: str, target: str, extra: str = "") -> Received:
    reader, writer = await open_connection(port)
    head_only = method == "HEAD"
    writer.write(f"{method} {target} HTTP/1.1\r\nhost: t\r\n{extra}\r\n".encode())
    received = await read_response(reader, head_only)
    writer.close()
    return received


async def eof(reader: asyncio.StreamReader, timeout: float = 5) -> bool:
    return await asyncio.wait_for(reader.read(1), timeout) == b""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def comparable(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers without the ones that differ per request (id, date)."""
    return [(k, v) for k, v in headers if k not in {"x-request-id", "date"}]


async def test_get_matches_the_asgi_answer(served: tuple[RustWorker, int]) -> None:
    _, port = served
    over_socket = await fetch(port, "GET", "/health")
    client = Client(make_app().asgi())
    await client.lifespan_event("startup")
    try:
        in_process = await client.request("GET", "/health")
    finally:
        await client.lifespan_event("shutdown")
    assert over_socket.status == in_process.status == 200
    assert over_socket.json() == in_process.body
    assert dict(comparable(over_socket.headers)) == {
        k: v for k, v in in_process.headers.items() if k not in {"x-request-id"}
    }
    assert len(over_socket.header("x-request-id")) == 32


async def test_the_native_404_and_405_carry_the_request_id(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    missing = await fetch(port, "GET", "/nope")
    assert missing.status == 404
    assert missing.json() == {
        "error": "not_found",
        "request_id": missing.header("x-request-id"),
    }
    wrong = await fetch(port, "DELETE", "/health")
    assert wrong.status == 405
    assert wrong.header("allow") == "GET, HEAD"
    assert wrong.header("x-request-id") != missing.header("x-request-id")


async def test_head_keeps_the_length_and_sends_no_body(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"HEAD /health HTTP/1.1\r\nhost: t\r\n\r\n")
    head = await read_response(reader, head_only=True)
    assert head.status == 200
    assert head.header("content-length") == "15"
    writer.write(b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")  # framing still intact
    assert (await read_response(reader)).json() == {"status": "ok"}
    writer.close()


async def test_a_non_ascii_target_gets_the_pipelines_400(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write("GET /caf\u00e9 HTTP/1.1\r\nhost: t\r\n\r\n".encode())
    refused = await read_response(reader)
    assert refused.status == 400
    assert refused.json()["error"] == "bad_path_encoding"
    assert refused.json()["request_id"] == refused.header("x-request-id")
    writer.close()


async def test_keep_alive_and_pipelining(served: tuple[RustWorker, int]) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    request = b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n"
    writer.write(request + request + b"GET /nope HTTP/1.1\r\nhost: t\r\n\r\n")
    statuses = [(await read_response(reader)).status for _ in range(3)]
    assert statuses == [200, 200, 404]
    writer.close()


async def test_a_post_without_a_body_reaches_the_handler(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    created = await fetch(port, "POST", "/made", "content-length: 0\r\n")
    assert (created.status, created.json()) == (201, {"ok": True})


async def test_a_request_with_a_body_is_refused_and_closed(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"POST /made HTTP/1.1\r\nhost: t\r\ncontent-length: 3\r\n\r\nabc")
    refused = await read_response(reader)
    assert refused.status == 501
    assert await eof(reader)
    writer.close()


async def test_oversized_query_and_headers_get_the_json_refusals() -> None:
    worker = RustWorker(
        make_app(max_query_bytes=64, max_header_bytes=2048), "127.0.0.1", 0
    )
    port = await worker.start()
    try:
        long_query = await fetch(port, "GET", "/health?" + "q=1&" * 40)
        assert (long_query.status, long_query.json()["error"]) == (414, "uri_too_long")
        big_header = await fetch(port, "GET", "/health", f"x: {'a' * 3000}\r\n")
        assert big_header.status == 431
        assert big_header.json()["error"] == "headers_too_large"
    finally:
        await worker.stop()


async def test_the_in_flight_bound_answers_503_without_running_the_handler() -> None:
    worker = RustWorker(
        make_app(sync_workers=1, sync_queue=0), "127.0.0.1", 0
    )  # in-flight bound 4
    port = await worker.start()
    try:
        held = [await open_connection(port) for _ in range(4)]
        for _, writer in held:
            writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
        while PROBE.entered < 4:
            await asyncio.sleep(0.01)
        busy = await fetch(port, "GET", "/block")
        assert busy.status == 503
        assert busy.json()["error"] == "overloaded"
        assert busy.header("retry-after") == "1"
        assert PROBE.entered == 4
        PROBE.gate.set()
        for reader, _ in held:
            assert (await read_response(reader)).status == 200
        assert worker.core.in_flight == 0
        for _, writer in held:
            writer.close()
    finally:
        await worker.stop()


async def test_the_loop_drains_until_the_queue_is_empty(
    served: tuple[RustWorker, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, port = served
    monkeypatch.setattr(rustserver, "DRAIN_BATCH", 2)
    answers = await asyncio.gather(*(fetch(port, "GET", "/health") for _ in range(25)))
    assert [a.status for a in answers] == [200] * 25


async def test_stop_lets_a_running_request_finish() -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0)
    port = await worker.start()
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.2)
    assert not stopping.done()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.wait_for(open_connection(port), 2)
    PROBE.gate.set()
    answer = await read_response(reader)
    assert (answer.status, answer.json()) == (200, {"released": "yes"})
    assert await eof(reader)
    assert await stopping == 0
    assert PROBE.shutdown_ran
    assert worker.core.in_flight == 0
    writer.close()


async def test_stop_aborts_what_outlives_the_shutdown_timeout() -> None:
    worker = RustWorker(make_app(shutdown_timeout=0.3), "127.0.0.1", 0)
    port = await worker.start()
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    assert await worker.stop() == 1
    assert await eof(reader)
    assert PROBE.cancelled == 1
    assert PROBE.shutdown_ran
    writer.close()


async def test_a_failing_startup_binds_nothing() -> None:
    app = Hinoki(include=[health], on_startup=[fail_startup])
    port = free_port()
    worker = RustWorker(app, "127.0.0.1", port)
    with pytest.raises(RuntimeError, match="startup failed"):
        await worker.start()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_a_busy_port_fails_and_closes_the_lifespan() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        worker = RustWorker(make_app(), "127.0.0.1", taken.getsockname()[1])
        with pytest.raises(RuntimeError):
            await worker.start()
    assert PROBE.shutdown_ran


async def test_trusted_proxies_are_refused_before_startup() -> None:
    settings = AppSettings(server=ServerSettings(trusted_proxies=("127.0.0.1",)))
    app = Hinoki(include=[health], settings=settings, on_shutdown=[note_shutdown])
    worker = RustWorker(app, "127.0.0.1", 0)
    with pytest.raises(RuntimeError, match="trusted_proxies"):
        await worker.start()
    assert not PROBE.shutdown_ran  # the lifespan never started


def spawn_server(port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hinoki.runtime.rustserver",
            "tests.rustserverapp:app",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        text=True,
    )


def blocking_get(port: int, path: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n".encode()
        )
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


async def test_sigterm_stops_the_process_after_the_running_request() -> None:
    port = free_port()
    process = spawn_server(port)
    try:
        assert process.stdout is not None
        line = await asyncio.to_thread(process.stdout.readline)
        assert line.startswith("listening on"), line
        health_answer = await asyncio.to_thread(blocking_get, port, "/health")
        assert health_answer.startswith(b"HTTP/1.1 200")
        slow = asyncio.create_task(asyncio.to_thread(blocking_get, port, "/slow"))
        await asyncio.sleep(0.2)
        process.send_signal(signal.SIGTERM)
        answer = await slow
        assert answer.startswith(b"HTTP/1.1 200")
        assert answer.endswith(b'{"slow":"done"}')
        assert await asyncio.to_thread(process.wait, 10) == 0
    finally:
        process.kill()
        process.wait()
