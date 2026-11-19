"""Trusted proxies and single routing in the Rust HTTP/1.1 server, over real sockets."""

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, Request, core, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.proxy import trusted_peer
from hinoki.runtime.servercore import build_server_core
from hinoki.settings import AppSettings, SecuritySettings, ServerSettings
from tests.kisoservertest import Received, fetch, read_response

FORWARDED = (
    "x-forwarded-proto: https\r\n"
    "x-forwarded-host: forged.test\r\n"
    "x-request-id: edge-42\r\n"
)


@http.get("/info")
async def info(request: Request) -> dict[str, Any]:
    return {
        "scheme": request.scheme,
        "client": request.client[0] if request.client else None,
        "host": request.host,
        "request_id": request.request_id,
    }


@http.get("/items/{name}")
async def item(name: str) -> dict[str, str]:
    return {"name": name}


def make_app(proxies: tuple[str, ...], **server: Any) -> Hinoki:
    settings = AppSettings(
        server=ServerSettings(trusted_proxies=proxies, **server),
        security=SecuritySettings(hsts_seconds=60),
    )
    return Hinoki(include=[info, item], settings=settings)


@asynccontextmanager
async def running(app: Hinoki) -> AsyncIterator[int]:
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        yield port
    finally:
        await worker.stop()


# The test client connects from 127.0.0.1.
TRUSTED = ("127.0.0.1",)
UNTRUSTED = ("10.0.0.0/8", "2001:db8::/32")


def hsts(answer: Received) -> bool:
    return any(k == "strict-transport-security" for k, _ in answer.headers)


async def test_a_trusted_peer_supplies_scheme_and_request_id() -> None:
    async with running(make_app(TRUSTED, trust_request_id=True)) as port:
        answer = await fetch(port, "GET", "/info", FORWARDED)
        missing = await fetch(port, "GET", "/nope", FORWARDED)
    assert answer.json() == {
        "scheme": "https",
        "client": "127.0.0.1",
        "host": "t",  # never the forwarded host
        "request_id": "edge-42",
    }
    assert answer.header("x-request-id") == "edge-42"
    assert hsts(answer)
    # The natively answered 404 follows the same rules without Python.
    assert missing.status == 404
    assert missing.header("x-request-id") == "edge-42"
    assert missing.json()["request_id"] == "edge-42"
    assert hsts(missing)


async def test_an_untrusted_peer_gets_no_forwarded_value() -> None:
    async with running(make_app(UNTRUSTED, trust_request_id=True)) as port:
        answer = await fetch(port, "GET", "/info", FORWARDED)
        missing = await fetch(port, "GET", "/nope", FORWARDED)
    assert answer.json()["scheme"] == "http"
    assert answer.json()["host"] == "t"
    assert answer.header("x-request-id") != "edge-42"
    assert not hsts(answer)
    assert missing.header("x-request-id") != "edge-42"
    assert not hsts(missing)


async def test_a_trusted_peer_needs_trust_request_id_for_the_id() -> None:
    async with running(make_app(TRUSTED)) as port:
        answer = await fetch(port, "GET", "/info", FORWARDED)
    assert answer.json()["scheme"] == "https"
    assert answer.header("x-request-id") != "edge-42"


async def test_no_proxies_configured_trusts_nobody() -> None:
    async with running(make_app((), trust_request_id=True)) as port:
        answer = await fetch(port, "GET", "/info", FORWARDED)
        missing = await fetch(port, "GET", "/nope", FORWARDED)
    assert answer.json()["scheme"] == "http"
    assert answer.header("x-request-id") != "edge-42"
    assert not hsts(missing)


@pytest.mark.parametrize(
    "extra",
    [
        "x-forwarded-proto: http\r\n",
        "x-forwarded-proto: wss\r\n",
        "x-forwarded-proto: https\r\nx-forwarded-proto: https\r\n",
    ],
)
async def test_only_one_valid_https_value_selects_https(extra: str) -> None:
    async with running(make_app(TRUSTED)) as port:
        answer = await fetch(port, "GET", "/info", extra)
        missing = await fetch(port, "GET", "/nope", extra)
    assert answer.json()["scheme"] == "http"
    assert not hsts(answer)
    assert not hsts(missing)


@pytest.mark.parametrize("bad", ["two,values", "has space", "x" * 129])
async def test_an_invalid_trusted_id_is_replaced_and_logged(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="hinoki.runtime")
    async with running(make_app(TRUSTED, trust_request_id=True)) as port:
        answer = await fetch(port, "GET", "/info", f"x-request-id: {bad}\r\n")
    assert answer.header("x-request-id") != bad
    assert "ignored invalid request ID from trusted proxy" in caplog.text


async def test_an_untrusted_id_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="hinoki.runtime")
    async with running(make_app(UNTRUSTED, trust_request_id=True)) as port:
        await fetch(port, "GET", "/info", "x-request-id: two,values\r\n")
    assert "ignored invalid request ID" not in caplog.text


async def test_a_trusted_forwarded_client_chain_reaches_the_handler() -> None:
    async with running(make_app(TRUSTED)) as port:
        answer = await fetch(
            port, "GET", "/info", "x-forwarded-for: 198.51.100.9, 127.0.0.1\r\n"
        )
    assert answer.json()["client"] == "198.51.100.9"


def decision_table() -> list[tuple[str, str]]:
    peers = ["10.1.2.3", "11.0.0.1", "::1", "2001:db8::5", "::ffff:10.1.2.3"]
    nets = ["10.0.0.0/8", "10.1.2.3", "2001:db8::/32", "::ffff:10.0.0.0/104", "::/0"]
    return [(peer, net) for peer in peers for net in nets]


@pytest.mark.parametrize(("peer", "net"), decision_table())
def test_the_native_and_python_trust_decisions_agree(peer: str, net: str) -> None:
    app = make_app((net,))
    pipeline = Pipeline(app)
    native = build_server_core(pipeline)
    assert isinstance(native, core.ServerCore)
    expected = trusted_peer({"client": (peer, 1)}, pipeline.trusted_proxies)
    assert native.trusts(peer) is expected


def test_invalid_native_entries_are_refused() -> None:

    with pytest.raises(ValueError, match="invalid trusted proxy"):
        core.ServerCore(
            [], [], ([], []), b"x-request-id", "0" * 16, trusted_proxies=["nope"]
        )


class CountingLookup:
    """Counts the Python route lookups an application performs."""

    def __init__(self, app: Hinoki) -> None:
        self.calls: list[tuple[str, str]] = []
        self.real: Callable[[str, str], Any] = app.lookup
        app.lookup = self  # type: ignore[method-assign]

    def __call__(self, method: str, path: str) -> Any:
        self.calls.append((method, path))
        return self.real(method, path)


@pytest.mark.parametrize("hosts", [False, True])
async def test_a_matched_route_is_not_looked_up_twice(hosts: bool) -> None:
    settings = AppSettings(allowed_hosts=("t",)) if hosts else None
    app = Hinoki(include=[info, item], settings=settings)
    lookups = CountingLookup(app)
    async with running(app) as port:
        got = await fetch(port, "GET", "/items/a%2Fb%20c")
        head = await fetch(port, "HEAD", "/items/x")
        info_answer = await fetch(port, "GET", "/info")
    assert got.status == 200
    assert got.json() == {"name": "a/b c"}
    assert head.status == 200
    assert info_answer.status == 200
    assert lookups.calls == []


async def test_a_bad_percent_encoding_in_a_matched_param_is_a_400() -> None:
    async with running(make_app(())) as port:
        answer = await fetch(port, "GET", "/items/%ff")
    assert answer.status == 400
    assert answer.json()["error"] == "bad_path_encoding"


async def test_unrouted_requests_are_still_looked_up_by_python() -> None:
    settings = AppSettings(allowed_hosts=("t",))
    app = Hinoki(include=[info, item], settings=settings)
    lookups = CountingLookup(app)
    async with running(app) as port:
        missing = await fetch(port, "GET", "/nope")
        wrong = await fetch(port, "DELETE", "/info")
        non_ascii = await fetch(port, "GET", "/caf%C3%A9")
    assert missing.status == 404
    assert wrong.status == 405
    assert wrong.header("allow") == "GET, HEAD"
    assert non_ascii.status == 404
    assert [path for _, path in lookups.calls] == ["/nope", "/info", "/caf%C3%A9"]


async def test_a_native_answer_reports_an_ignored_trusted_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="hinoki.runtime")
    worker = KisoWorker(make_app(TRUSTED, trust_request_id=True), "127.0.0.1", 0)
    port = await worker.start()
    try:
        for _ in range(2):
            missing = await fetch(port, "GET", "/nope", "x-request-id: two,values\r\n")
            assert missing.status == 404
        assert "ignored invalid request ID" not in caplog.text  # nothing per request
        worker.housekeeping()
        assert (
            "ignored invalid request ID from trusted proxy (2 natively" in caplog.text
        )
        caplog.clear()
        worker.housekeeping()  # the count was taken: nothing again
        assert "ignored invalid request ID" not in caplog.text
        # A queued request logs for itself and is not counted twice.
        await fetch(port, "GET", "/info", "x-request-id: two,values\r\n")
        worker.housekeeping()
        assert caplog.text.count("ignored invalid request ID") == 1
    finally:
        await worker.stop()


async def test_the_last_second_of_ignored_ids_is_reported_at_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="hinoki.runtime")
    worker = KisoWorker(make_app(TRUSTED, trust_request_id=True), "127.0.0.1", 0)
    port = await worker.start()
    await fetch(port, "GET", "/nope", "x-request-id: two,values\r\n")
    await worker.stop()  # no housekeeping pass has run since the request
    assert "ignored invalid request ID from trusted proxy (1 natively" in caplog.text


async def test_a_failing_housekeeping_pass_does_not_end_the_chain(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = KisoWorker(make_app(TRUSTED), "127.0.0.1", 0)
    await worker.start()
    try:
        worker.timer.cancel()  # type: ignore[union-attr]
        monkeypatch.setattr(worker, "housekeeping", lambda: 1 / 0)
        worker._tick()
        assert "housekeeping failed" in caplog.text
        assert worker.timer is not None and not worker.timer.cancelled()
    finally:
        monkeypatch.undo()
        await worker.stop()


TLS_DIR = Path(__file__).parent / "tls"


async def test_a_trusted_proxy_scheme_wins_over_tls_in_python_and_native_answers() -> (
    None
):
    worker = KisoWorker(
        make_app(TRUSTED),
        "127.0.0.1",
        0,
        tls_cert_file=str(TLS_DIR / "localhost-cert.pem"),
        tls_key_file=str(TLS_DIR / "localhost-key.pem"),
    )
    port = await worker.start()
    context = ssl.create_default_context(cafile=str(TLS_DIR / "localhost-cert.pem"))
    answers = []
    try:
        for path in ("/info", "/nope"):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=context, server_hostname="localhost"
            )
            writer.write(
                f"GET {path} HTTP/1.1\r\nhost: t\r\nx-forwarded-proto: http\r\n"
                "connection: close\r\n\r\n".encode()
            )
            answers.append(await read_response(reader))
            writer.close()
    finally:
        await worker.stop()
    python, native = answers
    assert python.json()["scheme"] == "http"
    assert not hsts(python)
    assert not hsts(native)  # the native 404 agrees with the Python answer
