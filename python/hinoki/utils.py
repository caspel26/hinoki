"""Small utilities shared by application code."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """A callable clock; inject one when time-dependent behavior needs control."""

    def __call__(self) -> datetime: ...


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _read_utc(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("a Hinoki clock must return a timezone-aware datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise ValueError("a Hinoki clock returned an invalid timezone") from exc
    if offset is None:
        raise ValueError("a Hinoki clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
