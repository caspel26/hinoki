"""A per-class acceptance check for schema instances built by Python code.

Constructing a ``Schema`` validates the final field values with the native
validator, which needs every value converted to wire primitives first. For the
common field types the answer is known without that detour: a value of exactly
the declared type that no constraint can refuse is valid. This module compiles,
once per class, a check that accepts only what the validator is certain to
accept; any value it cannot vouch for (another type, a constrained or
unsupported field, a missing attribute) makes the caller run the full
validation, so every refusal keeps its message, path and code.

Nested schemas are re-checked through their own compiled check on every
construction, so an instance changed after it was built is judged by what it
holds now, not by what it held when it was created."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from hinoki.fieldtypes import FieldType
    from hinoki.schema import Schema

Check = Callable[[Any], bool]
_UNBUILT = object()
_MISSING = object()
ONE_MINUTE = dt.timedelta(minutes=1)
_MAX_NESTING = 64
"""Static schema nesting the check covers; deeper graphs use the full validation."""


def instance_accepts(cls: type[Schema], instance: Schema) -> bool:
    """Whether ``instance`` is certainly valid; ``False`` means "run the full
    validation", not "invalid"."""
    check = cls.__dict__.get("__fast_check__", _UNBUILT)
    if check is _UNBUILT:
        check = build_instance_check(cls, ())
        cls.__fast_check__ = check  # type: ignore[attr-defined]
    return check is not None and check(instance)


def build_instance_check(cls: type[Schema], building: tuple[type, ...]) -> Check | None:
    """The check of one class, or ``None`` when a field is not covered or the
    schema graph is recursive or too deep."""
    if cls in building or len(building) >= _MAX_NESTING:
        return None
    inside = (*building, cls)
    fields: list[tuple[str, Check]] = []
    for name, spec in cls.specs().items():
        checker = field_check(spec.ftype, inside)
        if checker is None:
            return None
        fields.append((name, checker))
    plan = tuple(fields)

    def check(instance: Any) -> bool:
        for name, accepts in plan:  # noqa: SIM110 - a plain loop is cheaper than all()
            if not accepts(getattr(instance, name, _MISSING)):
                return False
        return True

    return check


def field_check(ftype: FieldType, building: tuple[type, ...]) -> Check | None:
    if ftype.enum_class is not None:
        return None
    accepts = kind_check(ftype, building)
    if accepts is None or not ftype.nullable:
        return accepts
    return lambda value: value is None or accepts(value)


def kind_check(ftype: FieldType, building: tuple[type, ...]) -> Check | None:
    if ftype.kind == "union":
        return None
    if ftype.schema is not None:
        return nested_check(ftype.schema, building)
    constraints = ftype.info.constraints()
    if ftype.kind == "list":
        return list_check(ftype, constraints, building)
    return SCALAR_CHECKS.get(ftype.kind, no_check)(constraints)


def no_check(_constraints: dict[str, object]) -> Check | None:
    return None


def nested_check(schema: type[Schema], building: tuple[type, ...]) -> Check | None:
    inner = build_instance_check(schema, building)
    if inner is None:
        return None
    return lambda value: type(value) is schema and inner(value)


def sized(constraints: dict[str, object]) -> tuple[int, int | None] | None:
    """``(min_length, max_length)``, or ``None`` if another constraint is set."""
    if constraints.keys() - {"min_length", "max_length"}:
        return None
    low, high = constraints.get("min_length"), constraints.get("max_length")
    if not isinstance(low or 0, int) or not isinstance(high or 0, int):
        return None
    return (low or 0, high)  # type: ignore[return-value]


def list_check(
    ftype: FieldType, constraints: dict[str, object], building: tuple[type, ...]
) -> Check | None:
    bounds = sized(constraints)
    if bounds is None or ftype.collection is not None or ftype.item is None:
        return None
    item = field_check(ftype.item, building)
    if item is None:
        return None
    low, high = bounds

    def accepts(value: Any) -> bool:
        return (
            type(value) is list
            and len(value) >= low
            and (high is None or len(value) <= high)
            and all(item(member) for member in value)
        )

    return accepts


def text_check(constraints: dict[str, object]) -> Check | None:
    bounds = sized(constraints)
    if bounds is None:
        return None
    low, high = bounds

    def accepts(value: Any) -> bool:
        if type(value) is not str or "\0" in value:
            return False
        if len(value) < low or (high is not None and len(value) > high):
            return False
        return value.isascii() or encodable(value)

    return accepts


def encodable(text: str) -> bool:
    """Whether ``text`` is valid Unicode (no lone surrogates)."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def int_check(constraints: dict[str, object]) -> Check | None:
    if constraints.keys() - {"ge", "le", "gt", "lt"}:
        return None
    if any(type(limit) is not int for limit in constraints.values()):
        return None
    ge, le = constraints.get("ge"), constraints.get("le")
    gt, lt = constraints.get("gt"), constraints.get("lt")
    if not constraints:
        return lambda value: type(value) is int

    def accepts(value: Any) -> bool:
        return (
            type(value) is int
            and (ge is None or value >= ge)  # type: ignore[operator]
            and (le is None or value <= le)  # type: ignore[operator]
            and (gt is None or value > gt)  # type: ignore[operator]
            and (lt is None or value < lt)  # type: ignore[operator]
        )

    return accepts


def float_check(constraints: dict[str, object]) -> Check | None:
    if constraints:
        return None
    return lambda value: type(value) is float and value - value == 0.0


def bool_check(constraints: dict[str, object]) -> Check | None:
    return None if constraints else lambda value: type(value) is bool


def uuid_check(constraints: dict[str, object]) -> Check | None:
    return None if constraints else lambda value: isinstance(value, UUID)


def datetime_check(constraints: dict[str, object]) -> Check | None:
    if constraints:
        return None
    return lambda value: (
        type(value) is dt.datetime
        and value.tzinfo is not None
        and whole_minutes(value.utcoffset())
    )


def whole_minutes(offset: dt.timedelta | None) -> bool:
    """Whether the UTC offset is one the validator reads (whole minutes)."""
    return offset is not None and offset % ONE_MINUTE == dt.timedelta(0)


def date_check(constraints: dict[str, object]) -> Check | None:
    return None if constraints else lambda value: type(value) is dt.date


SCALAR_CHECKS: dict[str, Callable[[dict[str, object]], Check | None]] = {
    "str": text_check,
    "int": int_check,
    "float": float_check,
    "bool": bool_check,
    "uuid": uuid_check,
    "datetime": datetime_check,
    "date": date_check,
}
