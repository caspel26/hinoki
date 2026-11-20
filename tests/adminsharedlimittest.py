"""The database-backed sign-in limiter: one window per client, shared by workers."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hinoki import RateLimitContext, errors
from hinoki.admin import users
from hinoki.admin.users import AdminAccounts, limits
from hinoki.admin.users.limits import DatabaseLoginWindow
from hinoki.admin.users.models import LoginWindowRow
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.utils import utc_now

MIGRATIONS = Path(users.__file__).parent / "migrations"


class Clock:
    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now


def context(client: str | None) -> RateLimitContext:
    return RateLimitContext("POST", "/admin/login", client, None, lambda kind: kind)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connected = await Database.connect(f"sqlite://{tmp_path / 'limit.sqlite3'}")
    await migrator.apply(connected, MIGRATIONS, namespace="admin")
    with use_database(connected):
        yield connected
    connected.close()


async def test_the_window_refuses_with_the_seconds_left_and_then_reopens(
    database: Database,
) -> None:
    clock = Clock()
    policy = DatabaseLoginWindow(limit=2, window=60, clock=clock)
    await policy.check(context("203.0.113.1"))
    await policy.check(context("203.0.113.1"))
    clock.now += timedelta(seconds=10)

    with pytest.raises(errors.TooManyRequests) as refused:
        await policy.check(context("203.0.113.1"))
    await policy.check(context("203.0.113.2"))  # another address has its own window
    clock.now += timedelta(seconds=60)
    await policy.check(context("203.0.113.1"))  # the window ended

    assert refused.value.retry_after == 51


async def test_two_policies_on_one_database_share_the_count(
    database: Database,
) -> None:
    first = DatabaseLoginWindow(limit=2, window=60, clock=Clock())
    second = DatabaseLoginWindow(limit=2, window=60, clock=Clock())
    await first.check(context("203.0.113.1"))
    await second.check(context("203.0.113.1"))

    with pytest.raises(errors.TooManyRequests):
        await first.check(context("203.0.113.1"))


async def test_an_ipv6_host_counts_by_its_64_prefix_and_unknown_clients_share(
    database: Database,
) -> None:
    policy = DatabaseLoginWindow(limit=1, window=60, clock=Clock())
    await policy.check(context("2001:db8:1:2::1"))
    with pytest.raises(errors.TooManyRequests):
        await policy.check(context("2001:db8:1:2:ffff::9"))  # same /64
    await policy.check(context("2001:db8:1:3::1"))

    for _ in range(limits.UNKNOWN_FACTOR):
        await policy.check(context(None))
    with pytest.raises(errors.TooManyRequests):
        await policy.check(context(None))


async def test_stored_keys_are_digests_not_addresses(database: Database) -> None:
    await DatabaseLoginWindow(clock=Clock()).check(context("203.0.113.1"))

    rows = await LoginWindowRow.all()
    assert len(rows) == 1 and "203.0.113.1" not in rows[0].key
    assert len(rows[0].key) == 64


async def test_expired_windows_are_removed(database: Database) -> None:
    clock = Clock()
    policy = DatabaseLoginWindow(limit=5, window=60, clock=clock)
    await policy.check(context("203.0.113.1"))
    clock.now += timedelta(seconds=120)
    await policy.check(context("203.0.113.2"))

    keys = [row.key for row in await LoginWindowRow.all()]
    assert len(keys) == 1


async def test_concurrent_checks_never_pass_more_than_the_limit(
    database: Database,
) -> None:
    policy = DatabaseLoginWindow(limit=5, window=60, clock=Clock())

    async def attempt() -> bool:
        try:
            await policy.check(context("203.0.113.9"))
        except errors.TooManyRequests:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(20)))
    assert 1 <= sum(outcomes) <= 5


async def test_accounts_accept_the_shared_policy(database: Database) -> None:
    shared = DatabaseLoginWindow(limit=1, clock=Clock())
    assert AdminAccounts(login_limit=shared).login_limit is shared
