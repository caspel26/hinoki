"""The database-backed sign-in limiter: one GCRA bucket per client and route,
shared by workers and kept across a restart."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hinoki import RateLimitContext, errors
from hinoki.admin import users
from hinoki.admin.users import AdminAccounts, limits
from hinoki.admin.users.limits import DatabaseLoginWindow
from hinoki.admin.users.models import AdminRateLimitRow
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.testing import ManualClock

MIGRATIONS = Path(users.__file__).parent / "migrations"


def clock() -> ManualClock:
    return ManualClock(datetime(2030, 1, 1, tzinfo=UTC))


def context(
    database: Database, client: str | None, route: str = "/admin/login"
) -> RateLimitContext:
    return RateLimitContext(
        "POST", route, client, None, lambda kind: database if kind is Database else kind
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connected = await Database.connect(f"sqlite://{tmp_path / 'limit.sqlite3'}")
    await migrator.apply(connected, MIGRATIONS, namespace="admin")
    with use_database(connected):
        yield connected
    connected.close()


async def test_the_bucket_refuses_with_the_seconds_left_and_then_refills(
    database: Database,
) -> None:
    now = clock()
    policy = DatabaseLoginWindow(limit=2, window=60, clock=now)
    await policy.check(context(database, "203.0.113.1"))
    await policy.check(context(database, "203.0.113.1"))
    now.advance(seconds=10)

    with pytest.raises(errors.TooManyRequests) as refused:
        await policy.check(context(database, "203.0.113.1"))
    await policy.check(context(database, "203.0.113.2"))  # its own bucket
    now.advance(seconds=60)
    await policy.check(context(database, "203.0.113.1"))  # refilled

    assert refused.value.retry_after == 20


async def test_the_sign_in_and_the_password_change_count_apart(
    database: Database,
) -> None:
    policy = DatabaseLoginWindow(limit=1, window=60, clock=clock())
    await policy.check(context(database, "203.0.113.1", "/admin/login"))

    await policy.check(context(database, "203.0.113.1", "/admin/password"))
    with pytest.raises(errors.TooManyRequests):
        await policy.check(context(database, "203.0.113.1", "/admin/login"))


async def test_two_policies_on_one_database_share_the_count(
    database: Database,
) -> None:
    first = DatabaseLoginWindow(limit=2, window=60, clock=clock())
    second = DatabaseLoginWindow(limit=2, window=60, clock=clock())
    await first.check(context(database, "203.0.113.1"))
    await second.check(context(database, "203.0.113.1"))

    with pytest.raises(errors.TooManyRequests):
        await first.check(context(database, "203.0.113.1"))


async def test_an_ipv6_host_counts_by_its_64_prefix_and_unknown_clients_share(
    database: Database,
) -> None:
    policy = DatabaseLoginWindow(limit=1, window=60, clock=clock())
    await policy.check(context(database, "2001:db8:1:2::1"))
    with pytest.raises(errors.TooManyRequests):
        await policy.check(context(database, "2001:db8:1:2:ffff::9"))  # same /64
    await policy.check(context(database, "2001:db8:1:3::1"))

    for _ in range(limits.UNKNOWN_FACTOR):
        await policy.check(context(database, None))
    with pytest.raises(errors.TooManyRequests):
        await policy.check(context(database, None))


async def test_stored_keys_are_digests_not_addresses(database: Database) -> None:
    await DatabaseLoginWindow(clock=clock()).check(context(database, "203.0.113.1"))

    rows = await AdminRateLimitRow.all()
    assert len(rows) == 1 and "203.0.113.1" not in rows[0].bucket
    assert len(rows[0].bucket) == 64


async def test_concurrent_checks_admit_exactly_the_limit(database: Database) -> None:
    # SQLite has one writer; give this concurrent stress case the documented
    # maximum storage wait instead of the 0.5s production default.
    policy = DatabaseLoginWindow(limit=5, window=60, clock=clock(), timeout=5)

    async def attempt() -> bool:
        try:
            await policy.check(context(database, "203.0.113.9"))
        except errors.TooManyRequests:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(outcomes) == 5


async def test_a_missing_table_refuses_the_attempt(tmp_path: Path) -> None:
    bare = await Database.connect(f"sqlite://{tmp_path / 'bare.sqlite3'}")
    try:
        policy = DatabaseLoginWindow(clock=clock())
        with pytest.raises(RuntimeError, match="rate limit"):
            await policy.check(context(bare, "203.0.113.1"))
    finally:
        bare.close()


async def test_accounts_accept_the_shared_policy(database: Database) -> None:
    shared = DatabaseLoginWindow(limit=1, clock=clock())
    assert AdminAccounts(login_limit=shared).login_limit is shared
