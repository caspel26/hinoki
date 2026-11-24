"""JSON-compatible value encoding and the Dumpable protocol."""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import PurePath
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from hinoki import core
from hinoki.fields import MASK, Secret

ENCODERS: dict[type, Callable[[Any], Any]] = {
    UUID: str,
    datetime: datetime.isoformat,
    dt.date: dt.date.isoformat,
    dt.time: dt.time.isoformat,
    dt.timedelta: core.wire_text,
    Decimal: core.wire_text,
    bytes: core.wire_text,
    IPv4Address: str,
    IPv6Address: str,
    IPv4Network: str,
    IPv6Network: str,
    Secret: lambda _secret: MASK,
}


@runtime_checkable
class Dumpable(Protocol):
    """Anything that can render itself as JSON-compatible data."""

    def dump(self) -> dict[str, Any]: ...


RANKS: dict[type, int] = {type(None): 0, bool: 1, int: 1, float: 1, str: 2}


def member_key(member: Any) -> tuple[Any, ...]:
    """A sort key that orders the members of a set the same in every process:
    an Enum by its value, a tuple or frozenset by its members, other values by
    their own order."""
    if isinstance(member, enum.Enum):
        member = member.value
    if isinstance(member, tuple):
        return (3, tuple(member_key(item) for item in member))
    if isinstance(member, frozenset):
        return (4, tuple(sorted(member_key(item) for item in member)))
    return (RANKS.get(type(member), 5), member)


def sorted_items(value: set[Any] | frozenset[Any]) -> list[Any]:
    """The members of a set in a fixed order when they can be compared, else
    as stored."""
    try:
        return sorted(value, key=member_key)
    except TypeError:
        return list(value)


def encode_items(
    value: list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any] | dict[str, Any],
) -> Any:
    """Encode a container's items recursively into JSON-compatible data."""
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return [encode(item) for item in sorted_items(value)]
    return [encode(item) for item in value]


def encode(value: object) -> Any:
    """JSON-compatible form of a value produced by a handler or schema. A tuple
    becomes a list, as the JSON encoder writes it as an array; a set becomes a
    sorted list."""
    if isinstance(value, Dumpable):
        return value.dump()
    if isinstance(value, list | tuple | set | frozenset | dict):
        return encode_items(value)
    encoder = ENCODERS.get(type(value))
    if encoder is None and isinstance(value, PurePath):
        return str(value)
    return encoder(value) if encoder else value
