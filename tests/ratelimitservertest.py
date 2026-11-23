"""The built-in limiter behind real servers: Kiso in this process, Kiso with two
workers and Granian with two workers, on one shared table."""

import asyncio
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from hinoki import Hinoki, RateLimit, RateLimitContext, errors, events, http
from hinoki import ratelimit_store as store
from hinoki.db import Database
from hinoki.events import RateLimited
from hinoki.orm import migrator
from hinoki.ratelimit import MemoryLimits
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.listener import open_listener
from hinoki.settings import AppSettings, MetricsSettings, ServerSettings
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch, record_lanes
from tests.ratelimitapp import create_app
from tests.runtime import harness

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = Path(store.__file__).parent / "migrations"
FORWARDED = "x-forwarded-for: 203.0.113.{}\r\n"


@pytest.fixture
async def table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "limits.sqlite3"
    database = await Database.connect(f"sqlite://{path}")
    await migrator.apply(database, MIGRATIONS, namespace="ratelimit")
    database.close()
    monkeypatch.setenv("RATE_DATABASE", str(path))
    return path


@pytest.fixture
async def worker(table: Path) -> AsyncIterator[tuple[KisoWorker, int]]:
    settings = AppSettings(server=ServerSettings(trusted_proxies=("127.0.0.1",)))
    server = KisoWorker(create_app(settings), "127.0.0.1", 0)
    port = await server.start()
    yield server, port
    await server.stop()


async def test_the_native_lane_stays_native_and_answers_with_the_headers(
    worker: tuple[KisoWorker, int],
) -> None:
    server, port = worker
    lanes = record_lanes(server)
    replies = [await fetch(port, "GET", "/shared") for _ in range(4)]

    assert [r.status for r in replies] == [200, 200, 200, 429]
    assert [r.header("ratelimit-limit") for r in replies] == ["3"] * 4
    assert [r.header("ratelimit-remaining") for r in replies] == ["2", "1", "0", "0"]
    assert int(replies[3].header("retry-after")) > 0
    assert replies[3].json()["error"] == "rate_limited"
    assert lanes == [True] * 4


async def test_the_python_lane_and_a_sync_handler_are_limited_too(
    worker: tuple[KisoWorker, int],
) -> None:
    server, port = worker
    lanes = record_lanes(server)
    python = [await fetch(port, "GET", "/python") for _ in range(3)]
    blocking = [await fetch(port, "GET", "/sync") for _ in range(3)]

    assert [r.status for r in python] == [200, 200, 429]
    assert python[0].header("ratelimit-remaining") == "1"
    assert [r.status for r in blocking] == [200, 200, 429]
    assert lanes[:3] == [False] * 3


async def test_clients_behind_a_trusted_proxy_have_their_own_buckets(
    worker: tuple[KisoWorker, int],
) -> None:
    _, port = worker
    first = [await fetch(port, "GET", "/memory", FORWARDED.format(1)) for _ in range(3)]
    second = await fetch(port, "GET", "/memory", FORWARDED.format(2))

    assert [r.status for r in first] == [200, 200, 429]
    assert second.status == 200


async def test_a_storage_failure_closes_or_opens_by_choice(
    worker: tuple[KisoWorker, int],
) -> None:
    _, port = worker
    closed = await fetch(port, "GET", "/closed")
    permitted = await fetch(port, "GET", "/open")

    assert closed.status == 503
    assert closed.json()["error"] == "rate_limit_unavailable"
    assert permitted.status == 200


async def test_the_metrics_count_decisions_by_outcome(table: Path) -> None:
    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    server = KisoWorker(create_app(), "127.0.0.1", 0, metrics=options)
    port = await server.start()
    try:
        for _ in range(4):
            await fetch(port, "GET", "/shared")
        await fetch(port, "GET", "/closed")
        await fetch(port, "GET", "/open")
        page = (await fetch(metrics_port, "GET", "/metrics")).body.decode()
    finally:
        await server.stop()

    series = "hinoki_rate_limit_decisions_total"
    assert f'{series}{{decision="allowed",worker="0"}} 3' in page
    assert f'{series}{{decision="refused",worker="0"}} 1' in page
    assert f'{series}{{decision="unavailable",worker="0"}} 1' in page
    assert f'{series}{{decision="failed_open",worker="0"}} 1' in page


class Policy:
    """An application's own policy: it refuses every second request, and breaks
    on the third."""

    def __init__(self) -> None:
        self.calls = 0

    async def check(self, context: RateLimitContext) -> None:
        self.calls += 1
        if self.calls == 2:
            raise errors.TooManyRequests(5)
        if self.calls == 3:
            raise RuntimeError("store down")


@http.get("/own", rate_limit=Policy())
async def own() -> dict[str, bool]:
    return {"ok": True}


async def test_an_applications_own_policy_is_counted_too() -> None:
    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    server = KisoWorker(Hinoki(include=[own]), "127.0.0.1", 0, metrics=options)
    port = await server.start()
    try:
        codes = [(await fetch(port, "GET", "/own")).status for _ in range(3)]
        page = (await fetch(metrics_port, "GET", "/metrics")).body.decode()
    finally:
        await server.stop()

    assert codes == [200, 429, 503]
    series = "hinoki_rate_limit_decisions_total"
    for decision in ("allowed", "refused", "unavailable"):
        assert f'{series}{{decision="{decision}",worker="0"}} 1' in page


async def test_a_refusal_reaches_the_rate_limited_handlers(table: Path) -> None:
    seen: list[RateLimited] = []
    handlers = events.Handlers()

    @handlers.after_commit(RateLimited)
    async def note(event: RateLimited) -> None:
        seen.append(event)

    async with ASGIClient(create_app(handlers=[handlers])) as client:
        # The in-process client has no address: the unknown-client quota is 10x.
        for _ in range(20):
            await client.get("/python")
        refused = await client.get("/python")
        await asyncio.sleep(0.05)

    assert refused.status == 429
    assert [(e.policy, e.route, e.method) for e in seen] == [
        ("RateLimit", "/python", "GET")
    ]


async def test_a_handler_for_refusals_takes_a_route_off_the_native_lane(
    table: Path,
) -> None:
    seen: list[RateLimited] = []
    handlers = events.Handlers()

    @handlers.after_commit(RateLimited)
    async def note(event: RateLimited) -> None:
        seen.append(event)

    settings = AppSettings(server=ServerSettings(trusted_proxies=("127.0.0.1",)))
    server = KisoWorker(create_app(settings, [handlers]), "127.0.0.1", 0)
    port = await server.start()
    try:
        replies = [await fetch(port, "GET", "/shared") for _ in range(4)]
        await asyncio.sleep(0.1)
    finally:
        await server.stop()
    control = KisoWorker(create_app(settings), "127.0.0.1", 0)
    await control.start()
    try:
        on_lane = control.lane is not None
    finally:
        await control.stop()

    assert [r.status for r in replies] == [200, 200, 200, 429]
    assert on_lane
    assert server.lane is None
    assert [(e.route, e.method) for e in seen] == [("/shared", "GET")]


async def test_the_openapi_document_lists_the_headers_only_when_asked(
    table: Path,
) -> None:
    document = create_app().openapi()
    shared = document["paths"]["/shared"]["get"]["responses"]
    plain = document["paths"]["/memory"]["get"]["responses"]

    assert set(shared["200"]["headers"]) == {
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "RateLimit-Reset",
    }
    assert "Retry-After" in shared["429"]["headers"]
    assert "headers" not in plain["200"]
    assert set(plain["429"]["headers"]) == {"Retry-After"}


def get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return int(r.status)
    except urllib.error.HTTPError as error:
        return error.code


def wait_until_ready(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if get(port, "/open") == 200:
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail("the server did not become ready")


@pytest.fixture
def served(table: Path, request: pytest.FixtureRequest) -> Iterator[int]:
    port = harness.free_port()
    environment = {**os.environ, "RATE_DATABASE": str(table)}
    if request.param == "kiso":
        process: subprocess.Popen[bytes] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hinoki",
                "serve",
                "tests.ratelimitapp:create_app",
                "--server",
                "kiso",
                "--port",
                str(port),
                "--workers",
                "2",
            ],
            cwd=ROOT,
            env=environment,
        )
    else:
        process = harness.spawn(
            "tests.ratelimitapp:create_app",
            table.parent / "unused.log",
            port,
            workers=2,
            factory=True,
            cwd=ROOT,
            RATE_DATABASE=str(table),
        )
    try:
        wait_until_ready(port)
        yield port
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)


@pytest.mark.runtime
@pytest.mark.parametrize("served", ["kiso", "granian"], indirect=True)
def test_two_workers_share_one_limit(served: int) -> None:
    codes = [get(served, "/shared") for _ in range(10)]

    assert codes.count(200) == 3
    assert codes.count(429) == 7


async def scrape(app: Hinoki, paths: list[str]) -> tuple[list[int], str]:
    """Serve ``app`` with metrics on one worker, request ``paths``, and return
    the statuses and the metrics page."""
    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    server = KisoWorker(app, "127.0.0.1", 0, metrics=options)
    port = await server.start()
    try:
        codes = [(await fetch(port, "GET", path)).status for path in paths]
        page = (await fetch(metrics_port, "GET", "/metrics")).body.decode()
    finally:
        await server.stop()
    return codes, page


async def test_a_later_worker_counts_into_its_own_metrics(table: Path) -> None:
    app = create_app()
    series = 'hinoki_rate_limit_decisions_total{decision="allowed",worker="0"}'

    _, first = await scrape(app, ["/shared"])
    _, second = await scrape(app, ["/shared", "/shared"])

    assert f"{series} 1" in first
    assert f"{series} 2" in second


@http.get("/counted", rate_limit=RateLimit(5, 60, storage=MemoryLimits()))
async def counted() -> dict[str, bool]:
    return {"ok": True}


async def test_a_worker_started_beside_another_takes_over_the_counting() -> None:
    series = 'hinoki_rate_limit_decisions_total{decision="allowed",worker="0"}'
    earlier = KisoWorker(Hinoki(include=[counted]), "127.0.0.1", 0)
    earlier_port = await earlier.start()
    try:
        await fetch(earlier_port, "GET", "/counted")
        _, page = await scrape(Hinoki(include=[counted]), ["/counted", "/counted"])
    finally:
        await earlier.stop()

    assert f"{series} 2" in page


def broken_key(_: RateLimitContext) -> str:
    raise RuntimeError("no key")


@http.get("/nokey", rate_limit=RateLimit(1, 60, storage=MemoryLimits(), key=broken_key))
async def nokey() -> dict[str, bool]:
    return {"ok": True}


@http.get(
    "/longkey",
    rate_limit=RateLimit(1, 60, storage=MemoryLimits(), key=lambda _: "x" * 5000),
)
async def longkey() -> dict[str, bool]:
    return {"ok": True}


async def test_a_failure_before_the_store_is_counted_as_unavailable() -> None:
    codes, page = await scrape(Hinoki(include=[nokey, longkey]), ["/nokey", "/longkey"])

    assert codes == [503, 503]
    series = "hinoki_rate_limit_decisions_total"
    assert f'{series}{{decision="unavailable",worker="0"}} 2' in page


async def test_a_storage_failure_is_counted_once(table: Path) -> None:
    _, page = await scrape(create_app(), ["/closed"])

    series = "hinoki_rate_limit_decisions_total"
    assert f'{series}{{decision="unavailable",worker="0"}} 1' in page
