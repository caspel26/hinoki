"""The default sign-in limiter: a bounded fixed window per client."""

import pytest

from hinoki import RateLimitContext, errors
from hinoki.admin.users import limits
from hinoki.admin.users.limits import (
    FREE_FAILURES,
    LoginWindow,
    UserBackoff,
    window_key,
)


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


def test_an_ipv6_host_counts_by_its_64_prefix() -> None:
    policy = LoginWindow(limit=1, window=60, clock=Clock())
    policy.check(context("2001:db8:1:2::1"))

    with pytest.raises(errors.TooManyRequests):
        policy.check(context("2001:db8:1:2:ffff::9"))  # same /64
    policy.check(context("2001:db8:1:3::1"))  # another /64
    assert window_key("not an address") == "not an address"


def test_clients_without_an_address_share_a_larger_window() -> None:
    policy = LoginWindow(limit=2, window=60, clock=Clock())

    for _ in range(2 * limits.UNKNOWN_FACTOR):
        policy.check(context(None))
    with pytest.raises(errors.TooManyRequests):
        policy.check(context(None))
    policy.check(context("203.0.113.1"))  # a known address is unaffected


def test_the_table_never_exceeds_its_bound_and_drops_the_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CLIENTS", 3)
    clock = Clock()
    policy = LoginWindow(limit=5, window=60, clock=clock)
    for index in range(5):  # all inside one window: nothing has expired
        clock.now += 1
        policy.check(context(f"198.51.100.{index}"))

    assert list(policy.windows) == [
        f"/admin/login|198.51.100.{index}" for index in (2, 3, 4)
    ]


def test_a_full_table_never_forgets_a_blocked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CLIENTS", 2)
    clock = Clock()
    policy = LoginWindow(limit=1, window=60, clock=clock)
    policy.check(context("198.51.100.1"))  # now blocked
    clock.now += 1
    policy.check(context("198.51.100.2"))

    with pytest.raises(errors.TooManyRequests):
        policy.check(context("198.51.100.3"))  # would have evicted the block
    with pytest.raises(errors.TooManyRequests):
        policy.check(context("198.51.100.1"))
    clock.now += 60
    policy.check(context("198.51.100.3"))  # the oldest block has expired


def test_expired_windows_leave_the_table() -> None:
    clock = Clock()
    policy = LoginWindow(limit=5, window=60, clock=clock)
    policy.check(context("198.51.100.1"))
    clock.now += 61

    policy.check(context("198.51.100.2"))

    assert list(policy.windows) == ["/admin/login|198.51.100.2"]


def test_sign_in_and_password_change_count_apart() -> None:
    policy = LoginWindow(limit=1, window=60, clock=Clock())
    policy.check(context("203.0.113.1"))
    other = RateLimitContext(
        "POST", "/admin/password", "203.0.113.1", None, lambda kind: kind
    )

    policy.check(other)  # its own window
    with pytest.raises(errors.TooManyRequests):
        policy.check(context("203.0.113.1"))


def test_a_user_is_delayed_after_repeated_failures_from_any_address() -> None:
    clock = Clock()
    backoff = UserBackoff(clock=clock)
    for _ in range(FREE_FAILURES):
        backoff.check("ada")
        backoff.failed("ada")
    backoff.check("ada")  # the free failures carry no delay
    backoff.failed("ada")

    with pytest.raises(errors.TooManyRequests):
        backoff.check("ada")
    backoff.check("grace")  # other names are unaffected
    clock.now += 3
    backoff.check("ada")
    backoff.succeeded("ada")
    assert "ada" not in backoff.failures
