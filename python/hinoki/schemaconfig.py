"""Typed, immutable configuration for Schema classes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SchemaExtra(StrEnum):
    """Policy for unknown string keys in decoded Schema input."""

    FORBID = "forbid"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaConfig:
    """Immutable class-level Schema behavior configuration."""

    extra: SchemaExtra = SchemaExtra.FORBID

    def __post_init__(self) -> None:
        if type(self.extra) is not SchemaExtra:
            raise TypeError("extra must be a SchemaExtra member")


__all__ = ["SchemaConfig", "SchemaExtra"]
