"""The blog's example login limiter: a bounded fixed window per client."""

import pytest
from blog import limits
from blog.limits import LoginAttempts

from hinoki import RateLimitContext, errors


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def context(client: str | None) -> RateLimitContext:
    return RateLimitContext("POST", "/auth/login", client, None, lambda kind: kind)


def test_the_limit_refuses_with_the_seconds_left_in_the_window() -> None:
    clock = Clock()
    policy = LoginAttempts(limit=2, window=60, clock=clock)

    policy.check(context("203.0.113.1"))
    policy.check(context("203.0.113.1"))
    clock.now += 10
    with pytest.raises(errors.TooManyRequests) as refused:
        policy.check(context("203.0.113.1"))

    assert refused.value.retry_after == 51
    policy.check(context("203.0.113.2"))  # another address has its own window


def test_the_window_resets_after_it_ends() -> None:
    clock = Clock()
    policy = LoginAttempts(limit=1, window=60, clock=clock)
    policy.check(context(None))
    clock.now += 60

    policy.check(context(None))  # an unknown address shares one window; it reopened


def test_the_table_stays_bounded_under_a_flood_of_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CLIENTS", 3)
    clock = Clock()
    policy = LoginAttempts(limit=5, window=60, clock=clock)
    for index in range(3):
        policy.check(context(f"198.51.100.{index}"))
    clock.now += 61  # the first three windows have ended

    policy.check(context("198.51.100.9"))

    assert list(policy.windows) == ["198.51.100.9"]
