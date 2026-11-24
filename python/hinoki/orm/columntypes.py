"""The SQL column type of a ``Model`` annotation, shared by registration
(which refuses a column no migration could create) and migration planning."""

from __future__ import annotations

import enum
import types
import typing
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any
from uuid import UUID

TYPES: dict[Any, str] = {
    UUID: "uuid",
    str: "text",
    int: "bigint",
    float: "double precision",
    bool: "boolean",
    datetime: "timestamptz",
    date: "text",
    time: "text",
    timedelta: "bigint",
    Decimal: "numeric",
    bytes: "bytea",
    dict: "jsonb",
    list: "jsonb",
    IPv4Address: "text",
    IPv6Address: "text",
    IPv4Network: "text",
    IPv6Network: "text",
}


def sql_type(kind: Any) -> str | None:
    """The column type of an annotation's base type; an ``Enum`` is stored as
    its values, ``bigint`` when they are all integers and ``text`` otherwise."""
    if isinstance(kind, type) and issubclass(kind, enum.Enum):
        values = [member.value for member in kind]
        return "bigint" if values and all(type(v) is int for v in values) else "text"
    return TYPES.get(kind)


def unstorable(hint: object) -> str | None:
    """Why no column type holds values of ``hint``, or ``None`` when one does.
    ``Annotated`` metadata and an optional ``| None`` do not change the type."""
    hint = strip_annotated(hint)
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        rest = [
            strip_annotated(a) for a in typing.get_args(hint) if a is not type(None)
        ]
        if len(rest) != 1:
            return "a union of several types has no column type"
        hint = rest[0]
    kind = typing.get_origin(hint) or hint
    if sql_type(kind) is None:
        label = getattr(kind, "__name__", None) or repr(kind)
        return f"no column type holds {label}"
    return None


def strip_annotated(hint: object) -> object:
    while typing.get_origin(hint) is typing.Annotated:
        hint = typing.get_args(hint)[0]
    return hint
