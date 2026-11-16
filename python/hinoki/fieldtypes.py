"""Normalisation of field annotations into a small, closed type model."""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, cast, get_args, get_origin
from uuid import UUID

from hinoki import core
from hinoki.errors import RegistrationError
from hinoki.fields import FieldInfo, Unset

if TYPE_CHECKING:
    from hinoki.schema import Schema

SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    UUID: "uuid",
    datetime: "datetime",
}

ALLOWED_CONSTRAINTS: dict[str, frozenset[str]] = {
    "str": frozenset({"min_length", "max_length", "pattern", "one_of"}),
    "int": frozenset({"ge", "le", "gt", "lt", "one_of"}),
    "float": frozenset({"ge", "le", "gt", "lt", "one_of"}),
    "list": frozenset({"min_length", "max_length"}),
}


@dataclass(frozen=True, slots=True)
class FieldType:
    """A supported field type: a kind plus nullability and constraints."""

    kind: str
    nullable: bool = False
    allows_unset: bool = False
    info: FieldInfo = field(default_factory=FieldInfo)
    item: FieldType | None = None
    schema: type[Schema] | None = None


def is_union(annotation: object) -> bool:
    return get_origin(annotation) in {typing.Union, types.UnionType}


def split_annotated(annotation: object, where: str = "field") -> tuple[Any, FieldInfo]:
    """Strip ``Annotated`` and merge its ``Field`` metadata."""
    merged: dict[str, object] = {}
    alias: str | None = None
    while get_origin(annotation) is Annotated:
        base, *extras = get_args(annotation)
        merged, alias = merge_field_metadata(extras, merged, alias, where)
        annotation = base
    return annotation, field_info_from_constraints(merged, alias)


def merge_field_metadata(
    extras: list[object],
    merged: dict[str, object],
    alias: str | None,
    where: str,
) -> tuple[dict[str, object], str | None]:
    """Combine one Annotated layer, rejecting ambiguous wire names."""
    for extra in extras:
        if not isinstance(extra, FieldInfo):
            continue
        merged = {**extra.constraints(), **merged}
        if extra.alias is not None:
            alias = merge_alias(alias, extra.alias, where)
    return merged, alias


def merge_alias(current: str | None, candidate: str, where: str) -> str:
    """Merge aliases from nested annotations if they describe one wire name."""
    if current is not None and current != candidate:
        raise RegistrationError(
            f"{where}: conflicting aliases {current!r} and {candidate!r}"
        )
    return candidate


def field_info_from_constraints(
    constraints: dict[str, object], alias: str | None
) -> FieldInfo:
    """Build typed metadata after precedence has been resolved."""
    return FieldInfo(
        min_length=cast(int | None, constraints.get("min_length")),
        max_length=cast(int | None, constraints.get("max_length")),
        pattern=cast(str | None, constraints.get("pattern")),
        ge=cast(float | None, constraints.get("ge")),
        le=cast(float | None, constraints.get("le")),
        gt=cast(float | None, constraints.get("gt")),
        lt=cast(float | None, constraints.get("lt")),
        one_of=cast(tuple[Any, ...] | None, constraints.get("one_of")),
        alias=alias,
    )


def check_pattern(pattern: str, where: str) -> None:
    """Valid in Python and in the Rust engine that enforces it (linear-time
    syntax: no look-around, back-references or verbose mode)."""
    try:
        core.check_pattern(pattern)
    except ValueError as exc:
        raise RegistrationError(f"{where}: unsupported pattern: {exc}") from exc
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RegistrationError(f"{where}: invalid pattern: {exc}") from exc


def check_limits(info: FieldInfo, where: str) -> None:
    for name in ("min_length", "max_length"):
        value = getattr(info, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**62
        ):
            raise RegistrationError(f"{where}: {name} must be an integer")
    for name in ("ge", "le", "gt", "lt"):
        value = getattr(info, name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float)
        ):
            raise RegistrationError(f"{where}: {name} must be a number")


def check_one_of(info: FieldInfo, kind: str, where: str) -> None:
    """``one_of`` is a non-empty tuple of values of the field's own type."""
    values = info.one_of
    if values is None:
        return
    kinds: dict[str, tuple[type, ...]] = {
        "str": (str,),
        "int": (int,),
        "float": (int, float),
    }
    wanted = kinds[kind]
    if not values or any(
        isinstance(v, bool) or not isinstance(v, wanted) for v in values
    ):
        raise RegistrationError(
            f"{where}: one_of needs a non-empty list of {kind} values"
        )


def parse_annotation(annotation: object, where: str) -> FieldType:
    """Turn an annotation into a ``FieldType`` or fail with a clear error."""
    base, info = split_annotated(annotation, where)
    nullable = allows_unset = False
    if is_union(base):
        members = list(get_args(base))
        nullable = type(None) in members
        allows_unset = Unset in members
        rest = [m for m in members if m is not type(None) and m is not Unset]
        if len(rest) != 1:
            raise RegistrationError(f"{where}: unsupported union {annotation!r}")
        base, inner = split_annotated(rest[0], where)
        if (
            info.alias is not None
            and inner.alias is not None
            and info.alias != inner.alias
        ):
            raise RegistrationError(
                f"{where}: conflicting aliases {inner.alias!r} and {info.alias!r}"
            )
        constraints = {**inner.constraints(), **info.constraints()}
        info = field_info_from_constraints(
            constraints,
            alias=info.alias if info.alias is not None else inner.alias,
        )
    return build(base, info, nullable, allows_unset, where)


def build(
    base: Any, info: FieldInfo, nullable: bool, allows_unset: bool, where: str
) -> FieldType:
    item: FieldType | None = None
    schema: type[Schema] | None = None
    if base in SCALARS:
        kind = SCALARS[base]
    elif get_origin(base) is list:
        kind = "list"
        item = parse_annotation(get_args(base)[0], f"{where}[]")
    elif isinstance(base, type) and hasattr(base, "__hinoki_schema__"):
        kind, schema = "schema", base
    else:
        raise RegistrationError(f"{where}: unsupported type {base!r}")
    if info.pattern is not None:
        check_pattern(info.pattern, where)
    check_limits(info, where)
    unsupported = set(info.constraints()) - ALLOWED_CONSTRAINTS.get(kind, frozenset())
    if unsupported:
        raise RegistrationError(
            f"{where}: constraint(s) {sorted(unsupported)} do not apply to {kind}"
        )
    if kind in {"str", "int", "float"}:
        check_one_of(info, kind, where)
    return FieldType(kind, nullable, allows_unset, info, item, schema)
