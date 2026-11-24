"""Instance-value extraction and type/container/union validation helpers."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from hinoki import core
from hinoki.errors import ErrorDetail, ValidationError
from hinoki.fields import UNSET, Secret
from hinoki.fieldtypes import DECIMAL_DIGITS_CEILING, FieldType
from hinoki.schema.decorators import _INSTANCE_MISSING, _INVALID_INSTANCE_VALUE

if TYPE_CHECKING:
    from hinoki.schema.base import Schema

_MAX_INSTANCE_DEPTH = 128
MAX_KEY_ECHO = 64
"""Characters of a client-chosen key that an error path repeats."""


def instance_error(path: str) -> ValidationError:
    """Build a depth-exceeded ValidationError for instance traversal."""
    return ValidationError([ErrorDetail(path, "depth", "input is nested too deeply")])


_PASSTHROUGH_KINDS = frozenset({"str", "int", "float", "bool"})


def instance_plan(
    cls: type[Schema],
) -> tuple[tuple[str, str, FieldType, bool], ...]:
    """Per class: ``(name, wire name, type, passes through)`` for each field.

    A plain ``str``/``int``/``float``/``bool`` field reaches the validator as
    it is, so it needs neither a path nor the typed conversion walk."""
    cached = cls.__dict__.get("__instance_plan__")
    if cached is None:
        cached = tuple(
            (
                name,
                spec.wire_name,
                spec.ftype,
                spec.ftype.kind in _PASSTHROUGH_KINDS
                and spec.ftype.schema is None
                and spec.ftype.enum_class is None,
            )
            for name, spec in cls.specs().items()
        )
        cls.__instance_plan__ = cached
    return cached


def schema_instance_values(
    cls: type[Schema],
    instance: Schema,
    *,
    use_aliases: bool = False,
    active: set[int] | None = None,
    depth: int = 0,
    path: str = "",
) -> dict[str, Any]:
    """Convert a bounded Python instance graph for Rust's validator."""
    if depth > _MAX_INSTANCE_DEPTH or (active is not None and id(instance) in active):
        raise instance_error(path)
    active = set() if active is None else active
    active.add(id(instance))
    try:
        values: dict[str, Any] = {}
        for name, wire_name, ftype, passes in instance_plan(cls):
            value = getattr(instance, name, _INSTANCE_MISSING)
            if value is _INSTANCE_MISSING:
                continue
            key = wire_name if use_aliases else name
            if passes and value is not UNSET:
                values[key] = value
                continue
            add_instance_value(
                values, key, ftype, value, (use_aliases, active, depth), path
            )
        return values
    finally:
        active.remove(id(instance))


def add_instance_value(
    values: dict[str, Any],
    key: str,
    ftype: FieldType,
    value: Any,
    walk: tuple[bool, set[int], int],
    path: str,
) -> None:
    use_aliases, active, depth = walk
    checked = instance_value(
        ftype,
        value,
        use_aliases=use_aliases,
        active=active,
        depth=depth + 1,
        path=f"{path}.{key}" if path else key,
    )
    if checked is not _INSTANCE_MISSING:
        values[key] = checked


def instance_value(
    ftype: FieldType,
    value: Any,
    *,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """Convert typed Python values into the Rust validator's wire primitives."""
    if value is UNSET:
        return _INSTANCE_MISSING if ftype.allows_unset else _INVALID_INSTANCE_VALUE
    if value is None:
        return None
    return typed_instance_value(
        ftype,
        value,
        use_aliases=use_aliases,
        active=active,
        depth=depth,
        path=path,
    )


def typed_instance_value(
    ftype: FieldType,
    value: Any,
    *,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """Convert one non-null, non-UNSET typed value for native validation."""
    if ftype.kind == "any":
        return value
    if ftype.kind == "anyof":
        return anyof_instance_value(ftype, value, use_aliases, active, depth, path)
    if ftype.kind == "union":
        checked = tagged_union_instance_value(
            ftype, value, use_aliases, active, depth, path
        )
    elif ftype.schema is not None:
        checked = nested_instance_value(
            ftype.schema, value, use_aliases, active, depth, path
        )
    elif ftype.kind in {"dict", "list", "tuple"}:
        checked = container_instance_value(
            ftype, value, use_aliases, active, depth, path
        )
    else:
        checked = scalar_instance_value(ftype, value)
    return checked


def scalar_instance_value(ftype: FieldType, value: Any) -> Any:
    if ftype.kind == "uuid":
        return str(value) if isinstance(value, UUID) else _INVALID_INSTANCE_VALUE
    if ftype.kind == "datetime":
        return (
            value.isoformat()
            if isinstance(value, datetime)
            else _INVALID_INSTANCE_VALUE
        )
    if ftype.kind in WIRE_KINDS:
        return wire_instance_value(ftype.kind, value)
    return member_instance_value(ftype, value)


PLAIN_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "uuid": UUID,
    "datetime": datetime,
    "date": dt.date,
    "time": dt.time,
    "timedelta": dt.timedelta,
    "decimal": Decimal,
    "bytes": bytes,
    "path": Path,
    "secret": Secret,
    "ipv4": IPv4Address,
    "ipv6": IPv6Address,
    "ipv4network": IPv4Network,
    "ipv6network": IPv6Network,
    "dict": dict,
    "tuple": tuple,
}
NOT_ACCEPTED: dict[str, type] = {"int": bool, "float": bool, "date": datetime}
"""Instances of a wanted type that a kind does not take: ``True`` is not an
integer or a number, a ``datetime`` is not a ``date``."""


def variant_accepts(ftype: FieldType, value: Any) -> bool:
    """Whether a constructed value is of the Python type this variant yields."""
    if ftype.kind == "any":
        return True
    if value is None:
        return ftype.nullable
    return type_accepts(ftype, value)


def type_accepts(ftype: FieldType, value: Any) -> bool:
    if ftype.kind == "anyof":
        return any(variant_accepts(item, value) for item in ftype.items)
    if ftype.kind == "union":
        return isinstance(value, tuple(schema for _, schema in ftype.variants))
    if ftype.schema is not None:
        return isinstance(value, ftype.schema)
    return member_accepts(ftype, value)


def member_accepts(ftype: FieldType, value: Any) -> bool:
    if ftype.enum_class is not None:
        return type(value) is ftype.enum_class
    wanted = PLAIN_TYPES.get(ftype.kind, ())
    if ftype.kind == "list":
        wanted = COLLECTION_TYPES.get(ftype.collection or "", list)
    return isinstance(value, wanted) and not isinstance(
        value, NOT_ACCEPTED.get(ftype.kind, ())
    )


def anyof_instance_value(
    ftype: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """A constructed value as the wire primitive of the first variant that takes
    its Python type; a value no variant takes is not accepted."""
    for variant in ftype.items:
        if variant_accepts(variant, value):
            return instance_value(
                variant,
                value,
                use_aliases=use_aliases,
                active=active,
                depth=depth,
                path=path,
            )
    return _INVALID_INSTANCE_VALUE


def member_instance_value(ftype: FieldType, value: Any) -> Any:
    """An Enum field takes only its own members; the validator reads the value."""
    if ftype.enum_class is None:
        return value
    return value.value if type(value) is ftype.enum_class else _INVALID_INSTANCE_VALUE


WIRE_KINDS = {
    "date": (dt.date, "a date"),
    "time": (dt.time, "a time"),
    "timedelta": (dt.timedelta, "a duration"),
    "decimal": (Decimal, "a decimal"),
    "bytes": (bytes, "bytes"),
    "ipv4": (IPv4Address, "an IPv4 address"),
    "ipv6": (IPv6Address, "an IPv6 address"),
    "ipv4network": (IPv4Network, "an IPv4 network"),
    "ipv6network": (IPv6Network, "an IPv6 network"),
    "path": (Path, "a path"),
    "secret": (Secret, "a secret string"),
}
OVERSIZED_DECIMAL = "9" * (DECIMAL_DIGITS_CEILING + 1)
TEXT_READERS: dict[str, Callable[[Any], str]] = {
    "ipv4": str,
    "ipv6": str,
    "ipv4network": str,
    "ipv6network": str,
    "path": str,
    "secret": Secret.reveal,
}
"""Kinds whose constructor value is read back as text by one plain call."""


def wire_instance_value(kind: str, value: Any) -> Any:
    """A ``date``, ``time``, ``timedelta``, ``Decimal`` or ``bytes`` value as the
    text the validator reads; a value of another type is not accepted."""
    wanted = WIRE_KINDS[kind][0]
    if not isinstance(value, wanted) or (
        (kind == "date" and isinstance(value, datetime))
        or (kind == "bytes" and type(value) is not bytes)
    ):
        return _INVALID_INSTANCE_VALUE
    reader = TEXT_READERS.get(kind)
    if reader is not None:
        return reader(value)
    try:
        text = core.wire_text(value)
    except ValueError:
        # not finite (no text form) or beyond the digit ceiling (over any bound)
        return (
            OVERSIZED_DECIMAL
            if cast(Decimal, value).is_finite()
            else _INVALID_INSTANCE_VALUE
        )
    return text


def tagged_union_instance_value(
    ftype: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    if not getattr(type(value), "__hinoki_schema__", False):
        return _INVALID_INSTANCE_VALUE
    variant = next(
        ((tag, schema) for tag, schema in ftype.variants if type(value) is schema),
        None,
    )
    if variant is None:
        return _INVALID_INSTANCE_VALUE
    tag, schema = variant
    discriminator = ftype.discriminator
    wire_name = ftype.discriminator_wire_name
    if discriminator is None or wire_name is None:
        return _INVALID_INSTANCE_VALUE
    if getattr(value, discriminator, _INSTANCE_MISSING) != tag:
        field_path = wire_name if use_aliases else discriminator
        where = f"{path}.{field_path}" if path else field_path
        raise ValidationError(
            [
                ErrorDetail(
                    where,
                    "discriminator_mismatch",
                    "value does not match its Schema variant",
                )
            ]
        )
    return schema_instance_values(
        schema,
        value,
        use_aliases=use_aliases,
        active=active,
        depth=depth,
        path=path,
    )


def nested_instance_value(
    schema: type[Schema],
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    if not isinstance(value, schema):
        return _INVALID_INSTANCE_VALUE
    return schema_instance_values(
        schema,
        value,
        use_aliases=use_aliases,
        active=active,
        depth=depth,
        path=path,
    )


COLLECTION_TYPES: dict[str, type] = {"set": set, "frozenset": frozenset, "tuple": tuple}


def container_instance_value(
    ftype: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """A list, dict, set, frozenset or tuple value as the JSON-like data the
    validator reads; a set or tuple field refuses any other container."""
    if ftype.kind == "dict" and ftype.item is not None:
        return mapping_instance_value(
            ftype.item, value, use_aliases, active, depth, path
        )
    if ftype.kind == "tuple":
        return fixed_instance_value(ftype, value, use_aliases, active, depth, path)
    wanted = COLLECTION_TYPES.get(ftype.collection or "", list)
    if not isinstance(value, wanted):
        return value if wanted is list else _INVALID_INSTANCE_VALUE
    items = list(cast(Iterable[Any], value))
    return list_instance_value(
        cast(FieldType, ftype.item), items, use_aliases, active, depth, path
    )


def fixed_instance_value(
    ftype: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """A fixed tuple as a list, each position converted by its own type; a
    value of the wrong length goes on as it is, for the validator to refuse."""
    if not isinstance(value, tuple):
        return _INVALID_INSTANCE_VALUE
    if depth > _MAX_INSTANCE_DEPTH:
        raise instance_error(path)
    if len(value) != len(ftype.items):
        return list(value)
    return [
        instance_value(
            item_type,
            item,
            use_aliases=use_aliases,
            active=active,
            depth=depth + 1,
            path=f"{path}[{index}]",
        )
        for index, (item_type, item) in enumerate(zip(ftype.items, value, strict=True))
    ]


def list_instance_value(
    item_type: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    if not isinstance(value, list):
        return value
    if depth > _MAX_INSTANCE_DEPTH:
        raise instance_error(path)
    return [
        instance_value(
            item_type,
            item,
            use_aliases=use_aliases,
            active=active,
            depth=depth + 1,
            path=f"{path}[{index}]",
        )
        for index, item in enumerate(value)
    ]


def mapping_instance_value(
    item_type: FieldType,
    value: Any,
    use_aliases: bool,
    active: set[int],
    depth: int,
    path: str,
) -> Any:
    """A dict's values converted like list items; keys are left for the Rust
    validator, which reports a key that is not a string."""
    if not isinstance(value, dict):
        return value
    if depth > _MAX_INSTANCE_DEPTH:
        raise instance_error(path)
    return {
        key: instance_value(
            item_type,
            item,
            use_aliases=use_aliases,
            active=active,
            depth=depth + 1,
            path=entry_path(path, key),
        )
        for key, item in value.items()
    }


def entry_path(path: str, key: object) -> str:
    """The error path of one dict entry: the key, shortened as the validator
    shortens it, as one more dotted segment."""
    return f"{path}.{str(key)[:MAX_KEY_ECHO]}" if path else str(key)[:MAX_KEY_ECHO]


def typed_entries(
    ftype: FieldType, value: Any, path: str
) -> list[tuple[FieldType, str, Any]]:
    """``(element type, path, element)`` of a list, dict, set or tuple field
    value; empty for anything else."""
    if ftype.kind == "anyof":
        chosen = next((v for v in ftype.items if variant_accepts(v, value)), None)
        return [] if chosen is None else [(chosen, path, value)]
    if ftype.kind == "tuple":
        return fixed_entries(ftype, value, path)
    if ftype.item is None:
        return []
    item_type = ftype.item
    return [(item_type, at, item) for at, item in container_entries(ftype, value, path)]


def fixed_entries(
    ftype: FieldType, value: Any, path: str
) -> list[tuple[FieldType, str, Any]]:
    if not isinstance(value, tuple) or len(value) != len(ftype.items):
        return []
    return [
        (child, f"{path}[{index}]", item)
        for index, (child, item) in enumerate(zip(ftype.items, value, strict=True))
    ]


def container_entries(ftype: FieldType, value: Any, path: str) -> list[tuple[str, Any]]:
    """``(path, element)`` of a list or dict field value; empty for anything else."""
    if ftype.kind == "dict" and isinstance(value, dict):
        return [(entry_path(path, key), item) for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        return [(f"{path}[{index}]", item) for index, item in enumerate(value)]
    return []
