"""``RateLimit``'s arguments, its keys and the signals it gives, in memory."""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hinoki import Caller, RateLimit, RateLimitContext, core, errors
from hinoki import ratelimit as module
from hinoki.db import Database
from hinoki.ratelimit import DatabaseLimits, MemoryLimits
from hinoki.testing import ManualClock

START = datetime(2030, 1, 1, tzinfo=UTC)


def memory(limit: int = 1, window: float = 60, **options: object) -> RateLimit:
    return RateLimit(
        limit,
        window,
        storage=MemoryLimits(),
        clock=ManualClock(START),
        **options,  # type: ignore[arg-type]
    )


def context(
    client: str | None = "203.0.113.1", caller: Caller[None] | None = None
) -> RateLimitContext:
    return RateLimitContext("GET", "/items", client, caller, lambda kind: kind)


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0, "window": 60},
        {"limit": True, "window": 60},
        {"limit": 1, "window": 1.5},
        {"limit": 1, "window": 0},
        {"limit": 1, "window": 86_401},
        {"limit": 1, "window": float("inf")},
        {"limit": 2, "window": 60, "cost": 3},
        {"limit": 1, "window": 60, "unknown_factor": 0},
        {"limit": 1, "window": 60, "on_error": "maybe"},
        {"limit": 1, "window": 60, "scope": ""},
        {"limit": 1, "window": 60, "key": 7},
        {"limit": 1, "window": 60, "tenant": 7},
        {"limit": 1, "window": 60, "clock": 7},
        {"limit": 1, "window": 60, "storage": MemoryLimits(max_rows=0)},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(table="Not Valid")},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(timeout=60)},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(timeout=0.0004)},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(timeout=True)},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(timeout=float("nan"))},
        {"limit": 2**32, "window": 86_400},
        {"limit": 1_000_001, "window": 1},
        {"limit": 1, "window": 60, "unknown_factor": 2**32},
        {"limit": 1, "window": 60, "storage": MemoryLimits(max_rows=2**63)},
        {"limit": 1, "window": 60, "storage": MemoryLimits(sweep_every=2**32)},
        {"limit": 1, "window": 60, "storage": DatabaseLimits(max_rows=2**63)},
    ],
)
def test_invalid_arguments_are_refused_at_construction(
    options: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="must"):
        RateLimit(**options)  # type: ignore[arg-type]


async def test_an_async_key_is_refused() -> None:
    async def key(_: RateLimitContext) -> str:
        return "x"

    with pytest.raises(ValueError, match="plain"):
        RateLimit(1, 60, key=key)  # type: ignore[arg-type]


async def test_a_caller_has_its_own_bucket_and_an_anonymous_request_the_client() -> (
    None
):
    policy = memory(key="caller")
    alice, bob = Caller("alice", None), Caller("bob", None)

    assert (await policy.decide(context(caller=alice))).allowed
    assert (await policy.decide(context(caller=bob))).allowed
    assert not (await policy.decide(context(caller=alice))).allowed
    assert (await policy.decide(context())).allowed
    assert not (await policy.decide(context())).allowed


async def test_a_tenant_and_a_scope_separate_or_share_buckets() -> None:
    clock = ManualClock(START)
    storage = MemoryLimits()

    def bucket(tenant: str, scope: str = "shared") -> RateLimit:
        return RateLimit(
            1, 60, scope=scope, tenant=lambda _: tenant, storage=storage, clock=clock
        )

    assert (await bucket("a").decide(context())).allowed
    assert not (await bucket("a").decide(context())).allowed
    assert (await bucket("b").decide(context())).allowed
    assert (await bucket("a", "other").decide(context())).allowed


async def test_the_method_is_part_of_the_default_scope() -> None:
    storage = MemoryLimits()
    clock = ManualClock(START)
    reads = RateLimit(2, 60, storage=storage, clock=clock)
    writes = RateLimit(1, 60, storage=storage, clock=clock)
    read, write = (
        context(),
        RateLimitContext("POST", "/items", "203.0.113.1", None, str),
    )

    assert (await writes.decide(write)).allowed
    assert [(await reads.decide(read)).allowed for _ in range(3)] == [True, True, False]
    assert not (await writes.decide(write)).allowed


async def test_a_release_leaves_memory_buckets_with_their_storage() -> None:
    policy = memory()
    await policy.decide(context())
    policy.release()

    assert (await policy.decide(context())).allowed is False


async def test_an_oversized_key_is_an_error_not_a_bucket() -> None:
    policy = memory(key=lambda _: "x" * 5000)

    with pytest.raises(ValueError, match="longer than"):
        await policy.decide(context())


async def test_a_naive_clock_is_refused() -> None:
    policy = RateLimit(
        1, 60, storage=MemoryLimits(), clock=lambda: datetime(2030, 1, 1)
    )

    with pytest.raises(ValueError, match="aware"):
        await policy.decide(context())


async def test_check_raises_the_refusal_with_its_headers() -> None:
    policy = memory(headers=True)
    await policy.check(context())

    with pytest.raises(errors.TooManyRequests) as refused:
        await policy.check(context())

    assert refused.value.retry_after == 60
    assert refused.value.headers["retry-after"] == "60"
    assert refused.value.headers["ratelimit-remaining"] == "0"


def test_check_sync_counts_on_a_thread_without_a_loop() -> None:
    policy = memory()
    policy.check_sync(context())

    with pytest.raises(errors.TooManyRequests):
        policy.check_sync(context())


async def test_a_storage_failure_that_is_admitted_is_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(module, "_fail_open_warner", module._Warner())
    monkeypatch.setattr(module, "_missing_table_warner", module._Warner())
    database = await Database.connect(f"sqlite://{tmp_path / 'bare.sqlite3'}")
    permissive = RateLimit(
        1, 60, storage=DatabaseLimits(table="absent"), on_error="open"
    )
    closed = RateLimit(1, 60, storage=DatabaseLimits(table="absent"))
    where = RateLimitContext("GET", "/x", "203.0.113.1", None, lambda _: database)
    try:
        with caplog.at_level(logging.WARNING, logger="hinoki.ratelimit"):
            for _ in range(3):
                assert (await permissive.decide(where)).failed_open
            for _ in range(3):
                with pytest.raises(core.RateLimitUnavailable):
                    await closed.decide(where)
    finally:
        database.close()

    messages = [record.getMessage() for record in caplog.records]
    assert sum("admitted" in m for m in messages) == 1
    assert sum("missing or out of date" in m for m in messages) == 1


async def test_check_sync_reports_unavailable_storage(tmp_path: Path) -> None:
    database = await Database.connect(f"sqlite://{tmp_path / 'bare.sqlite3'}")
    policy = RateLimit(1, 60, storage=DatabaseLimits(table="absent"))
    where = RateLimitContext("GET", "/x", "203.0.113.1", None, lambda _: database)
    try:
        with pytest.raises(core.RateLimitUnavailable):
            await asyncio.to_thread(policy.check_sync, where)
    finally:
        database.close()
