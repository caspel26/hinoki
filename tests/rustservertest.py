"""The Rust HTTP/1.1 server over real loopback sockets."""

import asyncio
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hinoki import Access, Hinoki, RuntimeConfig, Schema, http
from hinoki.modules import ModuleContext
from hinoki.request import Request
from hinoki.runtime import rustserver
from hinoki.runtime.rustserver import RustWorker
from tests.harness import Client
from tests.providers import TokenAuth

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


@http.get("/big")
async def big() -> dict[str, str]:
    return {"data": "x" * (64 * 1024 * 1024)}


@http.get("/scheme")
async def scheme_of(request: Request) -> dict[str, str]:
    return {"scheme": request.scheme}


@http.get("/host")
async def host_of(request: Request) -> dict[str, str]:
    return {"host": request.host}


@http.post("/made", status=201)
async def made(request: Request) -> dict[str, bool]:
    return {"ok": True}


class Note(Schema):
    title: str


@http.post("/notes", status=201)
async def note(request: Request, data: Note) -> dict[str, str]:
    return {"title": data.title}


@http.post("/secured", access=Access(auth=TokenAuth()))
async def secured(request: Request, data: Note) -> dict[str, str]:
    PROBE.entered += 1
    return {"title": data.title}


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
        include=[health, big, scheme_of, host_of, made, note, secured, block],
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


def post(path: str, body: bytes, extra: str = "") -> bytes:
    head = f"POST {path} HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
    return f"{head}content-length: {len(body)}\r\n{extra}\r\n".encode() + body


async def test_a_json_body_reaches_the_handler_and_the_connection_is_reused(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = json.dumps({"title": "caf\u00e9\nline"}, indent=2).encode()
    for _ in range(2):
        writer.write(post("/notes", body))
        created = await read_response(reader)
        assert (created.status, created.json()) == (201, {"title": "caf\u00e9\nline"})
        assert not any(k == "connection" for k, _ in created.headers)
    writer.close()


async def test_a_chunked_json_body_reaches_the_handler(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "chunky"}'
    chunks = b"".join(
        f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in (body[:7], body[7:])
    )
    head = "POST /notes HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
    writer.write(
        f"{head}transfer-encoding: chunked\r\n\r\n".encode() + chunks + b"0\r\n\r\n"
    )
    created = await read_response(reader)
    assert (created.status, created.json()) == (201, {"title": "chunky"})
    assert created.header("connection") == "close"
    assert await eof(reader)
    writer.close()


async def asgi_answer(method: str, path: str, **kwargs: Any) -> Any:
    client = Client(make_app(max_body_bytes=64).asgi())
    await client.lifespan_event("startup")
    try:
        return await client.request(method, path, **kwargs)
    finally:
        await client.lifespan_event("shutdown")


async def send_streaming(port: int, head: bytes, body: bytes) -> tuple[Received, bool]:
    """Send `body` while reading the answer, as a real client does."""
    reader, writer = await open_connection(port)
    writer.write(head)

    async def push() -> None:
        try:
            writer.write(body)
            await writer.drain()
        except ConnectionError:
            pass  # the server answered and closed first

    sender = asyncio.create_task(push())
    received = await read_response(reader)
    closed = await eof(reader)
    sender.cancel()
    writer.close()
    return received, closed


async def test_a_declared_length_over_the_limit_is_a_413_like_asgi() -> None:
    worker = RustWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        size = 256 * 1024
        head = f"POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: {size}\r\n\r\n"
        refused, closed = await send_streaming(port, head.encode(), b"x" * size)
    finally:
        await worker.stop()
    expected = await asgi_answer(
        "POST",
        "/notes",
        body=b"x" * 65,
        content_type="application/json",
        headers={"content-length": str(size)},
    )
    assert refused.status == expected.status == 413
    assert refused.json() == {
        **expected.body,
        "request_id": refused.json()["request_id"],
    }
    assert refused.header("connection") == "close"
    assert closed


async def test_a_chunked_total_over_the_limit_is_a_413() -> None:
    worker = RustWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        head = b"POST /notes HTTP/1.1\r\nhost: t\r\ntransfer-encoding: chunked\r\n\r\n"
        chunk = b"80\r\n" + b"y" * 128 + b"\r\n"
        refused, closed = await send_streaming(port, head, chunk * 2048 + b"0\r\n\r\n")
    finally:
        await worker.stop()
    assert refused.status == 413
    assert refused.json()["error"] == "payload_too_large"
    assert closed


async def test_the_limit_itself_is_accepted() -> None:
    body = json.dumps({"title": "a" * 40}).encode()
    worker = RustWorker(make_app(max_body_bytes=len(body)), "127.0.0.1", 0)
    port = await worker.start()
    try:
        accepted = await raw(port, post("/notes", body))
    finally:
        await worker.stop()
    assert accepted.status == 201


async def test_authentication_comes_before_the_size_check_and_no_continue_is_sent(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    head = (
        "POST /secured HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\n"
        "expect: 100-continue\r\n\r\n"
    )
    writer.write(head.encode())
    refused = await read_response(reader)  # a 100 would be read here instead
    assert (refused.status, refused.json()["error"]) == (401, "unauthorized")
    assert PROBE.entered == 0
    assert refused.header("connection") == "close"
    assert await eof(reader)
    writer.close()


async def test_continue_is_sent_once_the_request_is_authorized(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "ok"}'
    head = (
        "POST /secured HTTP/1.1\r\nhost: t\r\ncontent-type: application/json\r\n"
        f"authorization: Bearer editor-token\r\ncontent-length: {len(body)}\r\n"
        "expect: 100-continue\r\n\r\n"
    )
    writer.write(head.encode())
    interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert interim.startswith(b"HTTP/1.1 100")
    writer.write(body)
    done = await read_response(reader)
    assert (done.status, done.json()) == (200, {"title": "ok"})
    writer.close()


async def test_an_oversized_declared_body_is_refused_without_a_continue() -> None:
    worker = RustWorker(make_app(max_body_bytes=64), "127.0.0.1", 0)
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        head = (
            "POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: 9999\r\n"
            "expect: 100-continue\r\n\r\n"
        )
        writer.write(head.encode())
        refused = await read_response(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (refused.status, refused.json()["error"]) == (413, "payload_too_large")


async def test_a_stalled_body_is_a_408_before_the_request_timeout() -> None:
    worker = RustWorker(
        make_app(request_timeout=5), "127.0.0.1", 0, body_read_timeout=0.3
    )
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        writer.write(post("/notes", b'{"title": "slow"}')[:-6])
        stalled = await read_response(reader)
        assert await eof(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (stalled.status, stalled.json()["error"]) == (408, "request_timeout")
    assert stalled.header("connection") == "close"


TLS_DIR = Path(__file__).parent / "tls"


async def test_the_worker_serves_https_with_its_own_certificate() -> None:
    worker = RustWorker(
        make_app(),
        "127.0.0.1",
        0,
        tls_cert_file=str(TLS_DIR / "localhost-cert.pem"),
        tls_key_file=str(TLS_DIR / "localhost-key.pem"),
    )
    port = await worker.start()
    try:
        context = ssl.create_default_context(cafile=str(TLS_DIR / "localhost-cert.pem"))
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=context, server_hostname="localhost"
        )
        writer.write(b"GET /scheme HTTP/1.1\r\nhost: localhost\r\n\r\n")
        secure = await read_response(reader)
        writer.close()
        plain_reader, plain_writer = await open_connection(port)
        plain_writer.write(b"GET /scheme HTTP/1.1\r\nhost: localhost\r\n\r\n")
        refused = await asyncio.wait_for(plain_reader.read(100), 5)
        assert not refused.startswith(b"HTTP/")  # a TLS alert, never an answer
        plain_writer.close()
    finally:
        await worker.stop()
    assert (secure.status, secure.json()) == (200, {"scheme": "https"})


REAL_SCOPE_OF = rustserver.scope_of
SCOPES: list[dict[str, Any]] = []


def recording_scope_of(*args: Any, **kwargs: Any) -> dict[str, Any]:
    scope = REAL_SCOPE_OF(*args, **kwargs)
    SCOPES.append(scope)
    return scope


async def curl_status(port: int, protocol: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "curl",
        "-sk",
        protocol,
        "-o",
        "/dev/null",
        "-w",
        "%{http_version} %{http_code}",
        f"https://127.0.0.1:{port}/health",
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 20)
    return out.decode()


def curl_speaks_http2() -> bool:
    curl = shutil.which("curl")
    if curl is None:
        return False
    version = subprocess.run(
        [curl, "--version"], capture_output=True, text=True, check=False
    )
    return "HTTP2" in version.stdout


@pytest.mark.skipif(not curl_speaks_http2(), reason="curl without HTTP/2")
async def test_http2_reaches_the_pipeline_with_http_version_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SCOPES.clear()
    monkeypatch.setattr(rustserver, "scope_of", recording_scope_of)
    worker = RustWorker(
        make_app(),
        "127.0.0.1",
        0,
        tls_cert_file=str(TLS_DIR / "localhost-cert.pem"),
        tls_key_file=str(TLS_DIR / "localhost-key.pem"),
        http2=True,
    )
    port = await worker.start()
    try:
        over_h2 = await curl_status(port, "--http2")
        over_h1 = await curl_status(port, "--http1.1")
    finally:
        await worker.stop()
    assert (over_h2, over_h1) == ("2 200", "1.1 200")
    assert [(s["http_version"], s["scheme"]) for s in SCOPES] == [
        ("2", "https"),
        ("1.1", "https"),
    ]


async def test_a_client_that_stops_reading_frees_its_connection_slot() -> None:
    worker = RustWorker(
        make_app(), "127.0.0.1", 0, max_connections=1, write_timeout=0.3
    )
    port = await worker.start()
    try:
        _, stalled = await open_connection(port)
        stalled.write(b"GET /big HTTP/1.1\r\nhost: t\r\n\r\n")
        served = await fetch(port, "GET", "/health")
        stalled.close()
    finally:
        await worker.stop()
    assert served.status == 200


async def test_a_body_timeout_above_the_request_timeout_is_clamped() -> None:
    worker = RustWorker(
        make_app(request_timeout=1), "127.0.0.1", 0, body_read_timeout=60
    )
    port = await worker.start()
    try:
        reader, writer = await open_connection(port)
        writer.write(post("/notes", b'{"title": "slow"}')[:-6])
        stalled = await read_response(reader)
        writer.close()
    finally:
        await worker.stop()
    assert (stalled.status, stalled.json()["error"]) == (408, "request_timeout")


async def test_a_client_that_leaves_mid_body_does_not_reach_the_handler(
    served: tuple[RustWorker, int],
) -> None:
    worker, port = served
    _, writer = await open_connection(port)
    writer.write(
        post(
            "/secured", b'{"title": "gone"}', "authorization: Bearer editor-token\r\n"
        )[:-5]
    )
    await writer.drain()
    writer.close()
    # The server may not have taken the request yet: wait for the permit to
    # appear and go away again before reading the counter.
    seen = False
    for _ in range(200):
        seen = seen or bool(worker.core.in_flight)
        if seen and not worker.core.in_flight and not worker.core.pending:
            break
        await asyncio.sleep(0.025)
    assert worker.core.in_flight == 0
    assert PROBE.entered == 0
    assert (await fetch(port, "GET", "/health")).status == 200


async def test_a_disconnected_body_is_the_asgi_invalid_body_error() -> None:
    failure = rustserver.body_failure("disconnected")
    assert (failure.code, failure.message) == (  # type: ignore[attr-defined]
        "disconnected",
        "the client disconnected",
    )


async def test_malformed_body_framing_keeps_the_disconnected_code() -> None:
    failure = rustserver.body_failure("malformed")
    assert (failure.code, failure.message) == (  # type: ignore[attr-defined]
        "disconnected",
        "the request body is malformed",
    )


async def test_a_bare_lf_in_a_header_is_refused_before_the_body(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    attack = (
        b"POST /secured HTTP/1.1\r\nhost: t\r\nx: a\n"
        b"authorization: Bearer editor-token\r\ncontent-length: 5\r\n\r\nabcde"
    )
    reader, writer = await open_connection(port)
    writer.write(attack)
    refused = await read_response(reader)
    assert refused.status == 400
    assert await eof(reader)
    writer.close()
    assert PROBE.entered == 0


async def test_content_length_with_transfer_encoding_is_refused(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    smuggle = (
        b"POST /notes HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\n"
        b"transfer-encoding: chunked\r\n\r\n0\r\n\r\n"
        b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n"
    )
    reader, writer = await open_connection(port)
    writer.write(smuggle)
    refused = await read_response(reader)
    assert refused.status == 400
    assert await eof(reader)
    writer.close()


async def test_a_request_pipelined_after_a_refused_body_is_never_answered(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    body = b'{"title": "x"}'
    head = f"POST /secured HTTP/1.1\r\nhost: t\r\ncontent-length: {len(body)}\r\n\r\n"
    writer.write(head.encode() + body + b"GET /health HTTP/1.1\r\nhost: t\r\n\r\n")
    refused = await read_response(reader)
    assert refused.status == 401
    assert refused.header("connection") == "close"
    assert await asyncio.wait_for(reader.read(), 5) == b""
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
        with pytest.raises(OSError, match="in use"):
            await worker.start()
    assert PROBE.shutdown_ran


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


async def raw(port: int, request: bytes) -> Received:
    reader, writer = await open_connection(port)
    writer.write(request)
    received = await read_response(reader)
    writer.close()
    return received


async def test_a_client_that_closes_its_write_side_still_gets_the_answer(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"GET /block HTTP/1.1\r\nhost: t\r\n\r\n")
    writer.write_eof()
    while PROBE.entered < 1:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)  # the server has seen the EOF
    PROBE.gate.set()
    answer = await read_response(reader)
    assert (answer.status, answer.json()) == (200, {"released": "yes"})
    writer.close()


@pytest.mark.parametrize(
    "head", ["", "host: a\r\nhost: b\r\n"], ids=["missing", "repeated"]
)
async def test_http_1_1_needs_exactly_one_host(
    served: tuple[RustWorker, int], head: str
) -> None:
    _, port = served
    refused = await raw(port, f"GET /health HTTP/1.1\r\n{head}\r\n".encode())
    assert refused.status == 400
    assert refused.json() == {
        "error": "bad_host",
        "request_id": refused.header("x-request-id"),
    }


async def test_an_absolute_form_target_makes_its_authority_the_host(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    answer = await raw(
        port, b"GET http://evil.example/host HTTP/1.1\r\nhost: other\r\n\r\n"
    )
    assert answer.json() == {"host": "evil.example"}


async def test_http_1_0_without_host_falls_back_to_the_listening_address(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    answer = await raw(port, b"GET /host HTTP/1.0\r\n\r\n")
    assert answer.json() == {"host": f"127.0.0.1:{port}"}


class FlakyCore:
    """The worker's core, with ``drain`` failing once."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failed = False

    def drain(self, limit: int) -> Any:
        if not self.failed:
            self.failed = True
            raise RuntimeError("drain failed")
        return self.inner.drain(limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_a_failing_drain_is_retried_instead_of_stranding_the_queue(
    served: tuple[RustWorker, int],
) -> None:
    worker, port = served
    worker.core = FlakyCore(worker.core)  # type: ignore[assignment]
    assert (await fetch(port, "GET", "/health")).status == 200


class FailingSpawn:
    """Stands in for ``RustWorker._spawn``: fails the first request, then
    restores the real method."""

    def __init__(self, worker: RustWorker, monkeypatch: pytest.MonkeyPatch) -> None:
        self.worker = worker
        self.monkeypatch = monkeypatch
        self.original = worker._spawn

    def __call__(self, request: Any) -> None:
        self.monkeypatch.setattr(self.worker, "_spawn", self.original)
        raise RuntimeError("no task")


async def test_a_request_that_cannot_be_started_gets_a_500_and_the_rest_flow(
    served: tuple[RustWorker, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, port = served
    monkeypatch.setattr(worker, "_spawn", FailingSpawn(worker, monkeypatch))
    failed = await fetch(port, "GET", "/health")
    assert failed.status == 500
    assert failed.json() == {
        "error": "internal_error",
        "request_id": failed.header("x-request-id"),
    }
    assert (await fetch(port, "GET", "/health")).status == 200


class CountingWake:
    """A wake callback that counts its calls."""

    def __init__(self, worker: RustWorker) -> None:
        self.worker = worker
        self.calls = 0

    def __call__(self, server: Any) -> None:
        self.calls += 1
        self.worker._wake(server)


async def test_the_wake_reader_is_removed_when_the_server_ends() -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0)
    await worker.start()
    server = worker.server
    assert server is not None
    # Replace the reader with a counting one, then end the server without stop().
    counting = CountingWake(worker)
    loop = asyncio.get_running_loop()
    loop.remove_reader(server.wake_fd)
    loop.add_reader(server.wake_fd, counting, server)
    try:
        await server.shutdown()
        await asyncio.sleep(0.3)
        assert counting.calls <= 2
    finally:
        await worker.stop()


async def test_a_connection_limit_beyond_the_semaphore_is_a_value_error() -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0, max_connections=2**62)
    with pytest.raises(ValueError, match="max_connections"):
        await worker.start()
    assert PROBE.shutdown_ran


class BrokenStop:
    async def wait(self) -> None:
        raise RuntimeError("interrupted")


async def test_run_stops_the_server_when_it_fails_after_start() -> None:
    worker = RustWorker(make_app(), "127.0.0.1", 0)

    worker.stopping = BrokenStop()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="interrupted"):
        await worker.run()
    assert worker.server is None
    assert PROBE.shutdown_ran


BARE_LF = b" HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 5\r\n\r\nhello"
CL_AND_TE = (
    b" HTTP/1.1\r\nhost: t\r\ncontent-length: 4\r\ntransfer-encoding: chunked\r\n\r\n"
    b"0\r\n\r\nGET /health HTTP/1.1\r\nhost: t\r\n\r\n"
)


@pytest.mark.parametrize("size", [41_000, 45_000, 48_000])
@pytest.mark.parametrize("tail", [BARE_LF, CL_AND_TE], ids=["bare_lf", "cl_te"])
async def test_an_oversized_head_cannot_hide_smuggling(
    served: tuple[RustWorker, int], size: int, tail: bytes
) -> None:
    _, port = served
    reader, writer = await open_connection(port)
    writer.write(b"POST /secured/" + b"a" * size + tail)  # one write
    answers = []
    try:
        while True:
            answers.append(await read_response(reader))
    except (asyncio.IncompleteReadError, ConnectionError):
        pass  # the connection ended
    writer.close()
    assert [a.status for a in answers] in ([], [400], [431])
    assert PROBE.entered == 0


async def test_a_head_under_the_limits_still_works(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    ok = await fetch(port, "GET", "/health", f"x: {'a' * 30_000}\r\n")
    assert ok.status == 200
    plan = await fetch(port, "GET", "/health", f"x: {'a' * 33_000}\r\n")
    assert plan.status == 431
    assert plan.json()["error"] == "headers_too_large"


async def test_a_refused_head_is_answered_before_the_client_stops_sending(
    served: tuple[RustWorker, int],
) -> None:
    _, port = served
    head = b"POST /notes HTTP/1.1\r\nhost: t\r\nx: a\ncontent-length: 262144\r\n\r\n"
    refused, closed = await send_streaming(port, head, b"x" * 262_144)
    assert refused.status == 400
    assert closed
