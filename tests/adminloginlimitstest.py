"""The default sign-in limiter: a bounded fixed window per client."""

import pytest

from hinoki import RateLimitContext, errors
from hinoki.admin.users import limits
from hinoki.admin.users.limits import LoginWindow


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def context(client: str | None) -> RateLimitContext:
    return RateLimitContext("POST", "/admin/login", client, None, lambda kind: kind)


def test_the_window_refuses_with_the_seconds_left_and_then_reopens() -> None:
    clock = Clock()
    policy = LoginWindow(limit=2, window=60, clock=clock)
    policy.check(context("203.0.113.1"))
    policy.check(context("203.0.113.1"))
    clock.now += 10

    with pytest.raises(errors.TooManyRequests) as refused:
        policy.check(context("203.0.113.1"))
    policy.check(context("203.0.113.2"))  # another address has its own window
    clock.now += 60
    policy.check(context("203.0.113.1"))  # the window ended

    assert refused.value.retry_after == 51


def test_the_table_stays_bounded_under_a_flood_of_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CLIENTS", 3)
    clock = Clock()
    policy = LoginWindow(limit=5, window=60, clock=clock)
    for index in range(3):
        policy.check(context(f"198.51.100.{index}"))
    clock.now += 61

    policy.check(context(None))

    assert list(policy.windows) == ["unknown"]
