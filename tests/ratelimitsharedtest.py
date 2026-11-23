"""The shared rate limiter: its guarantees on every database backend, from one
process and from several."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hinoki import RateLimit, RateLimitContext, core
from hinoki import ratelimit_store as store
from hinoki.db import Database
from hinoki.orm import migrator
from hinoki.ratelimit import DatabaseLimits
from hinoki.testing import ManualClock

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = Path(store.__file__).parent / "migrations"
START = datetime(2030, 1, 1, tzinfo=UTC)
BACKENDS = [
    "sqlite",
    "HINOKI_TEST_DATABASE_URL",
    "HINOKI_TEST_MYSQL_URL",
    "HINOKI_TEST_MARIADB_URL",
]


def clock() -> ManualClock:
    return ManualClock(START)


def context(database: Database, client: str | None = "203.0.113.1") -> RateLimitContext:
    return RateLimitContext(
        "GET",
        "/items",
        client,
        None,
        lambda kind: database if kind is Database else kind,
    )


def backend_url(name: str, tmp_path: Path) -> str:
    if name == "sqlite":
        return f"sqlite://{tmp_path / 'limits.sqlite3'}"
    url = os.environ.get(name)
    if url is None:
        pytest.skip(f"{name} is not set")
    return url


async def count(database: Database) -> int:
    rows = await database.fetch("SELECT COUNT(*) AS n FROM hinoki_rate_limits")
    return int(rows[0]["n"])


@pytest.fixture(params=BACKENDS)
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    connected = await Database.connect(backend_url(request.param, tmp_path), max_size=4)
    await migrator.apply(connected, MIGRATIONS, namespace="ratelimit")
    await connected.execute("DELETE FROM hinoki_rate_limits")
    yield connected
    connected.close()


async def test_a_burst_is_admitted_then_refused_with_the_time_to_wait(
    database: Database,
) -> None:
    now = clock()
    policy = RateLimit(3, 60, clock=now)
    seen = [await policy.decide(context(database)) for _ in range(4)]

    assert [d.allowed for d in seen] == [True, True, True, False]
    assert [d.remaining for d in seen] == [2, 1, 0, 0]
    assert [d.reset for d in seen[:3]] == [20, 40, 60]
    assert seen[3].retry_after == 20

    now.advance(seconds=20)
    assert (await policy.decide(context(database))).allowed


async def test_a_refusal_consumes_nothing(database: Database) -> None:
    now = clock()
    policy = RateLimit(1, 60, clock=now)
    await policy.decide(context(database))
    for _ in range(5):
        assert not (await policy.decide(context(database))).allowed

    now.advance(seconds=60)
    assert (await policy.decide(context(database))).allowed


async def test_clients_and_routes_have_separate_buckets(database: Database) -> None:
    policy = RateLimit(1, 60, clock=clock())
    other = RateLimit(1, 60, scope="other", clock=clock())

    assert (await policy.decide(context(database, "203.0.113.1"))).allowed
    assert (await policy.decide(context(database, "203.0.113.2"))).allowed
    assert (await other.decide(context(database, "203.0.113.1"))).allowed
    assert not (await policy.decide(context(database, "203.0.113.1"))).allowed
    assert await count(database) == 3


async def test_the_same_path_under_two_methods_has_two_quotas(
    database: Database,
) -> None:
    reads = RateLimit(3, 60, clock=clock())
    writes = RateLimit(1, 60, clock=clock())
    read = context(database)
    write = RateLimitContext("POST", "/items", "203.0.113.1", None, read.resource)

    assert (await writes.decide(write)).allowed
    assert [(await reads.decide(read)).allowed for _ in range(4)] == [True] * 3 + [
        False
    ]
    assert not (await writes.decide(write)).allowed
    assert await count(database) == 2


async def test_the_state_column_is_indexed(database: Database) -> None:
    queries = {
        "sqlite": "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'hinoki_rate_limits' AND sql LIKE '%(state)%'",
        "postgres": "SELECT COUNT(*) AS n FROM pg_indexes "
        "WHERE tablename = 'hinoki_rate_limits' AND indexdef LIKE '%(state)%'",
    }
    mysql = (
        "SELECT COUNT(*) AS n FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'hinoki_rate_limits' "
        "AND column_name = 'state'"
    )
    rows = await database.fetch(queries.get(database.dialect, mysql))

    assert int(rows[0]["n"]) >= 1


async def test_a_custom_key_is_normalised(database: Database) -> None:
    names = iter(["Alice", " alice ", "%41LICE", "bob"])
    policy = RateLimit(1, 60, key=lambda _: next(names), clock=clock())

    assert (await policy.decide(context(database))).allowed
    assert not (await policy.decide(context(database))).allowed
    assert not (await policy.decide(context(database))).allowed
    assert (await policy.decide(context(database))).allowed


async def test_an_unknown_client_has_a_wider_shared_quota(database: Database) -> None:
    policy = RateLimit(1, 60, unknown_factor=3, clock=clock())
    admitted = [await policy.decide(context(database, None)) for _ in range(4)]

    assert [d.allowed for d in admitted] == [True, True, True, False]


async def test_stored_buckets_are_digests_not_addresses(database: Database) -> None:
    await RateLimit(5, 60, clock=clock()).decide(context(database, "203.0.113.55"))

    rows = await database.fetch("SELECT bucket FROM hinoki_rate_limits")
    assert len(rows) == 1
    bucket = str(rows[0]["bucket"])
    assert len(bucket) == 64 and "203" not in bucket
    int(bucket, 16)


async def test_a_new_limiter_on_the_same_table_keeps_the_count(
    database: Database,
) -> None:
    await RateLimit(1, 3600, clock=clock()).decide(context(database))

    restarted = RateLimit(1, 3600, clock=clock())
    assert not (await restarted.decide(context(database))).allowed


async def test_concurrent_hits_admit_exactly_the_limit(database: Database) -> None:
    policy = RateLimit(10, 3600, clock=clock())
    seen = await asyncio.gather(*(policy.decide(context(database)) for _ in range(40)))

    assert sum(d.allowed for d in seen) == 10


async def test_sync_checks_from_threads_admit_exactly_the_limit(
    database: Database,
) -> None:
    policy = RateLimit(7, 3600, clock=clock())
    seen = await asyncio.gather(
        *(asyncio.to_thread(policy.decide_sync, context(database)) for _ in range(30))
    )

    assert sum(d.allowed for d in seen) == 7


async def test_a_sync_check_is_refused_on_the_event_loop(database: Database) -> None:
    with pytest.raises(RuntimeError):
        RateLimit(1, 60, clock=clock()).decide_sync(context(database))


async def test_a_missing_table_fails_closed_or_open(database: Database) -> None:
    storage = DatabaseLimits(table="absent_table")
    closed = RateLimit(1, 60, storage=storage, clock=clock())
    permissive = RateLimit(1, 60, storage=storage, on_error="open", clock=clock())

    with pytest.raises(core.RateLimitUnavailable):
        await closed.decide(context(database))
    decision = await permissive.decide(context(database))
    assert decision.allowed and decision.failed_open


async def test_expired_rows_are_swept_and_the_table_is_capped(
    database: Database,
) -> None:
    now = clock()
    storage = DatabaseLimits(max_rows=2, sweep_every=1)
    policy = RateLimit(1, 60, storage=storage, clock=now)
    assert (await policy.decide(context(database, "203.0.113.1"))).allowed
    assert (await policy.decide(context(database, "203.0.113.2"))).allowed

    full = await policy.decide(context(database, "203.0.113.3"))
    assert not full.allowed and full.retry_after == 60
    assert await count(database) == 2

    now.advance(seconds=61)
    assert (await policy.decide(context(database, "203.0.113.3"))).allowed
    assert await count(database) <= 2


def test_several_processes_share_one_limit(tmp_path: Path) -> None:
    path = tmp_path / "processes.sqlite3"
    url = f"sqlite://{path}"

    async def prepare() -> None:
        database = await Database.connect(url)
        await migrator.apply(database, MIGRATIONS, namespace="ratelimit")
        database.close()

    asyncio.run(prepare())
    workers = [
        subprocess.Popen(
            [sys.executable, "-m", "tests.ratelimitprocess", url, "12", "20"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    admitted = 0
    for worker in workers:
        output, _ = worker.communicate(timeout=60)
        assert worker.returncode == 0
        admitted += int(output.strip())

    assert admitted == 12
