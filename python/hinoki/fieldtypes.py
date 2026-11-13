"""Normalisation of field annotations into a small, closed type model."""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin
from uuid import UUID

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
    "str": frozenset({"min_length", "max_length", "pattern"}),
    "int": frozenset({"ge", "le", "gt", "lt"}),
    "float": frozenset({"ge", "le", "gt", "lt"}),
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


def split_annotated(annotation: object) -> tuple[Any, FieldInfo]:
    """Strip ``Annotated`` and merge its ``Field`` metadata."""
    merged: dict[str, object] = {}
    while get_origin(annotation) is Annotated:
        base, *extras = get_args(annotation)
        for extra in extras:
            if isinstance(extra, FieldInfo):
                merged = {**extra.constraints(), **merged}
        annotation = base
    return annotation, FieldInfo(**merged)  # type: ignore[arg-type]


def parse_annotation(annotation: object, where: str) -> FieldType:
    """Turn an annotation into a ``FieldType`` or fail with a clear error."""
    base, info = split_annotated(annotation)
    nullable = allows_unset = False
    if is_union(base):
        members = list(get_args(base))
        nullable = type(None) in members
        allows_unset = Unset in members
        rest = [m for m in members if m is not type(None) and m is not Unset]
        if len(rest) != 1:
            raise RegistrationError(f"{where}: unsupported union {annotation!r}")
        base, inner = split_annotated(rest[0])
        info = FieldInfo(**{**inner.constraints(), **info.constraints()})  # type: ignore[arg-type]
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
        try:
            re.compile(info.pattern)
        except re.error as exc:
            raise RegistrationError(f"{where}: invalid pattern: {exc}") from exc
    unsupported = set(info.constraints()) - ALLOWED_CONSTRAINTS.get(kind, frozenset())
    if unsupported:
        raise RegistrationError(
            f"{where}: constraint(s) {sorted(unsupported)} do not apply to {kind}"
        )
    return FieldType(kind, nullable, allows_unset, info, item, schema)
