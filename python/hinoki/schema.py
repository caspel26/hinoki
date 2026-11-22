"""Schemas: typed, immutable input/output models with strict validation."""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import functools
import inspect
import types
import typing
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path, PurePath
from typing import (
    Annotated,
    Any,
    ClassVar,
    Protocol,
    Self,
    cast,
    dataclass_transform,
    runtime_checkable,
)
from uuid import UUID

from hinoki import core
from hinoki.computed import (
    COMPUTED_TABLE,
    ComputedSpec,
    check_wire_names,
    collect_computed,
)
from hinoki.core import dump_instance
from hinoki.errors import (
    ErrorDetail,
    RegistrationError,
    ValidationError,
)
from hinoki.fields import MASK, UNSET, Field, FieldInfo, SecretStr, Unset
from hinoki.fieldtypes import (
    DECIMAL_DIGITS_CEILING,
    FieldType,
    normalize_text,
    parse_annotation,
)
from hinoki.schemaconfig import SchemaConfig, SchemaExtra
from hinoki.schemafast import instance_accepts, is_stock_validation

_NO_MODEL = object()


class _ConfigOmitted:
    """Private marker used to type the omitted class config keyword."""


_CONFIG_UNSET = _ConfigOmitted()
_SUPPLIED_FIELDS = "__hinoki_supplied_fields__"
_TRACKS_SUPPLIED_FIELDS = "__hinoki_tracks_supplied_fields__"
_TRUSTED_CONSTRUCTOR = "__hinoki_construct_validated__"
_MODEL_CONSTRUCTOR = "__hinoki_construct_from_model__"
_INSTANCE_MISSING = object()
_INVALID_INSTANCE_VALUE = object()
_NORMALIZATION_QUEUE: ContextVar[list[tuple[Schema, bool, str]] | None] = ContextVar(
    "hinoki_schema_normalization_queue", default=None
)

__all__ = [
    "UNSET",
    "Field",
    "FieldSpec",
    "Invalid",
    "Schema",
    "SchemaConfig",
    "SchemaExtra",
    "Unset",
    "field_check",
    "field_normalize",
    "object_check",
]


class ForwardReference(Exception):
    """Internal: an annotation names a class that is not defined yet."""


class Invalid(Exception):
    """Raised by a check to refuse a value: a stable ``code``, a readable
    ``message`` and optionally the ``path`` it concerns (default: the field the
    check is attached to, or the object itself)."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


CHECK_ATTR = "__hinoki_check__"
NORMALIZE_ATTR = "__hinoki_normalize__"
BEFORE_ATTR = "__hinoki_before__"
MAX_BEFORE_DEPTH = 64
NORMALIZER_PARAMETER_COUNT = 2


def field_check[F: Callable[..., Any]](*names: str) -> Callable[[F], F]:
    """Mark a method as a check on one or more fields: ``def ok(self, value)``.

    It runs after the structural validation succeeded, so ``value`` already has
    the declared type, and may read the other fields through ``self``. Raise
    ``Invalid(code, message)`` to refuse; it never changes the value. Checks are
    synchronous and must not do I/O: a rule that needs the database belongs in an
    operation, not in a schema."""
    if not names:
        raise RegistrationError("@field_check needs at least one field name")

    def mark(function: F) -> F:
        setattr(function, CHECK_ATTR, ("field", names))
        return function

    return mark


def object_check[F: Callable[..., Any]](function: F) -> F:
    """Mark a method as a check on the whole object: ``def ok(self)``. Runs after
    every field check passed or failed (all problems are reported together)."""
    setattr(function, CHECK_ATTR, ("object", ()))
    return function


def before_validate[F: Callable[..., Any]](function: F) -> F:
    """Mark a classmethod or staticmethod that reshapes raw wire input:
    ``def prepare(cls, data: dict[str, Any]) -> dict[str, Any]``.

    It runs on the decoded mapping, before any structural validation, for the
    schema and for every schema nested in it (lists, dicts, tuples, optionals and
    discriminated variants). Return the mapping to validate. The hook gets a shallow
    copy: replace nested values instead of mutating them, or the caller's input
    changes. Raise ``Invalid(code, message)`` to refuse; a ``KeyError``,
    ``TypeError``, ``ValueError``, ``IndexError`` or ``AttributeError`` caused by
    unexpected input answers as ``invalid_input``. The input
    is untrusted: check types before relying on them, stay synchronous and do no
    I/O. Plain unions of schemas that use it are refused: use a discriminator."""
    target = descriptor_function(function)
    if target is function:
        raise RegistrationError(
            "@before_validate must wrap a classmethod or staticmethod"
        )
    setattr(target, BEFORE_ATTR, True)
    return function


def field_normalize[F: Callable[..., str]](name: str) -> Callable[[F], F]:
    """Mark a typed field canonicalizer: ``def trim(self, value: str) -> str``.

    Methods run synchronously after structural validation and return a
    replacement string; raise ``Invalid`` only from a check hook to reject.
    """
    if not isinstance(name, str) or not name:
        raise RegistrationError("@field_normalize needs one non-empty field name")

    def mark(function: F) -> F:
        setattr(function, NORMALIZE_ATTR, name)
        return function

    return mark


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Introspection record of one schema field."""

    name: str
    alias: str | None
    ftype: FieldType
    required: bool

    @property
    def wire_name(self) -> str:
        return self.alias or self.name


def merge_alias(
    schema_name: str, field_name: str, annotated: str | None, assigned: str | None
) -> str | None:
    """Resolve aliases from Annotated metadata and an assigned Field value."""
    if annotated is not None and assigned is not None and annotated != assigned:
        raise RegistrationError(
            f"{schema_name}.{field_name}: conflicting aliases "
            f"{annotated!r} and {assigned!r}"
        )
    return annotated if annotated is not None else assigned


def validate_aliases(cls: type[Schema], specs: dict[str, FieldSpec]) -> None:
    """Reject ambiguous wire names and paths at schema declaration."""
    owners: dict[str, str] = {}
    for spec in specs.values():
        alias = spec.alias
        if alias is not None and (
            not isinstance(alias, str)
            or not alias
            or any(char in alias for char in ".[]")
            or any(unicodedata.category(char) == "Cc" for char in alias)
        ):
            raise RegistrationError(
                f"{cls.__name__}.{spec.name}: alias must be non-empty and "
                "cannot contain '.', '[', ']', or control characters"
            )
        wire_name = spec.wire_name
        previous = owners.get(wire_name)
        if previous is not None:
            raise RegistrationError(
                f"{cls.__name__}: fields {previous!r} and {spec.name!r} "
                f"share wire name {wire_name!r}"
            )
        owners[wire_name] = spec.name


def validate_reserved_names(cls: type, annotations: dict[str, Any]) -> None:
    """Keep tracking internals and the controlled-export method out of fields."""
    tracking_names = {
        _SUPPLIED_FIELDS,
        _TRACKS_SUPPLIED_FIELDS,
        _TRUSTED_CONSTRUCTOR,
        _MODEL_CONSTRUCTOR,
    }
    declared_internal = tracking_names | {"__schema_config__"}
    if declared_internal & (set(cls.__dict__) | set(annotations)):
        raise RegistrationError(
            f"{cls.__name__}: supplied-field tracking metadata is reserved by Hinoki"
        )
    inherited_fields: set[str] = set()
    inherited_annotations: set[str] = set()
    inherited_schema_config = False
    for base in cls.__mro__[1:]:
        base_annotations = inspect.get_annotations(base)
        inherited_annotations.update(base_annotations)
        if (
            not getattr(base, "__hinoki_schema__", False)
            and "__schema_config__" in base_annotations
        ):
            inherited_schema_config = True
        if dataclasses.is_dataclass(base):
            inherited_fields.update(item.name for item in dataclasses.fields(base))
    for method in ("dump", "replace"):
        if method in annotations or method in inherited_fields:
            raise RegistrationError(
                f"{cls.__name__}.{method}: field name conflicts with the "
                f"Schema.{method}() method; rename the Python field"
            )
    if tracking_names & (inherited_fields | inherited_annotations) or (
        "__schema_config__" in inherited_fields or inherited_schema_config
    ):
        raise RegistrationError(
            f"{cls.__name__}: supplied-field tracking metadata is reserved by Hinoki"
        )


def register_computed(cls: type[Schema], annotations: dict[str, Any]) -> None:
    """Collect the computed fields of ``cls`` and publish them for the Rust
    serializer (names are checked against the declared fields here, wire names
    once the fields are built)."""
    declared = set(annotations)
    for base in cls.__mro__[1:]:
        if dataclasses.is_dataclass(base):
            declared.update(item.name for item in dataclasses.fields(base))
    specs = collect_computed(cls, tuple(cls.__computed_specs__.values()), declared)
    cls.__computed_specs__ = specs
    cls.__computed_checked__ = False
    setattr(cls, COMPUTED_TABLE, tuple((s.name, s.wire_name) for s in specs.values()))


def resolve_schema_config(
    cls: type, configured: SchemaConfig | _ConfigOmitted
) -> SchemaConfig:
    """Resolve one immutable config, requiring explicit multiple-base choices."""
    if configured is not _CONFIG_UNSET:
        if not isinstance(configured, SchemaConfig):
            raise RegistrationError(f"{cls.__name__}: config must be a SchemaConfig")
        return configured
    inherited = {
        getattr(base, "__schema_config__", SchemaConfig())
        for base in cls.__bases__
        if getattr(base, "__hinoki_schema__", False)
    }
    if len(inherited) > 1:
        raise RegistrationError(
            f"{cls.__name__}: Schema bases have conflicting configs; pass config="
        )
    return next(iter(inherited), SchemaConfig())


def validate_normalizer_method(cls: type, method_name: str) -> None:
    descriptor = inspect.getattr_static(cls, method_name, None)
    if isinstance(descriptor, (staticmethod, classmethod)):
        raise RegistrationError(
            f"{cls.__name__}.{method_name}: field normalizers must be instance methods"
        )
    method = getattr(cls, method_name, None)
    if (
        not callable(method)
        or inspect.iscoroutinefunction(method)
        or inspect.isasyncgenfunction(method)
    ):
        raise RegistrationError(
            f"{cls.__name__}.{method_name}: field normalizers must be sync methods"
        )
    parameters = tuple(inspect.signature(method).parameters.values())
    if len(parameters) != NORMALIZER_PARAMETER_COUNT or any(
        parameter.kind
        not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
        or parameter.default is not inspect.Parameter.empty
        for parameter in parameters
    ):
        raise RegistrationError(
            f"{cls.__name__}.{method_name}: expected (self, value: str) -> str"
        )
    try:
        hints = typing.get_type_hints(method, include_extras=True)
    except (NameError, TypeError) as exc:
        raise RegistrationError(
            f"{cls.__name__}.{method_name}: normalizer annotations must resolve"
        ) from exc
    value_hint = hints.get(parameters[1].name, parameters[1].annotation)
    return_hint = hints.get("return", inspect.signature(method).return_annotation)
    if not annotation_is_str(value_hint) or not annotation_is_str(return_hint):
        raise RegistrationError(
            f"{cls.__name__}.{method_name}: expected (self, value: str) -> str"
        )


def annotation_is_str(annotation: Any) -> bool:
    if typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation is str


_MAX_INSTANCE_DEPTH = 128
MAX_KEY_ECHO = 64
"""Characters of a client-chosen key that an error path repeats."""


def instance_error(path: str) -> ValidationError:
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
    "secret": SecretStr,
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
    "secret": (SecretStr, "a secret string"),
}
OVERSIZED_DECIMAL = "9" * (DECIMAL_DIGITS_CEILING + 1)
TEXT_READERS: dict[str, Callable[[Any], str]] = {
    "ipv4": str,
    "ipv6": str,
    "ipv4network": str,
    "ipv6network": str,
    "path": str,
    "secret": SecretStr.get_secret_value,
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
    if not isinstance(value, Schema):
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


def validate_constructed(
    instance: Schema, *, from_model: bool = False, use_aliases: bool = False
) -> None:
    """Validate values and custom checks after construction has completed."""
    cls = type(instance)
    if not instance_accepts(cls, instance):
        cls._validate_instance(
            instance,
            allow_naive_datetime=from_model,
            use_aliases=use_aliases,
        )
    normalize_floats(instance)
    queue = _NORMALIZATION_QUEUE.get()
    if queue is not None:
        queue_normalization(queue, instance, use_aliases, "")
        return
    normalize_constructed(instance, use_aliases=use_aliases)
    if graph_has_checks(cls):
        found = run_checks(instance, "", use_aliases=use_aliases)
        if found:
            raise ValidationError(found)


def normalize_constructed(
    instance: Schema, *, use_aliases: bool, path_prefix: str = ""
) -> None:
    """Stage and validate canonical strings before committing any field."""
    proposed = prepare_normalized_values(instance, use_aliases, path_prefix)
    commit_normalized_values(instance, proposed)


def contains_float(ftype: FieldType) -> bool:
    """Whether a field holds ``float`` values directly or inside a container."""
    if ftype.kind == "float":
        return True
    if ftype.kind == "tuple":
        return any(contains_float(item) for item in ftype.items)
    return ftype.item is not None and contains_float(ftype.item)


def floated(ftype: FieldType, value: Any) -> Any:
    """``value`` with every ``int`` where the type says ``float`` made a ``float``."""
    if ftype.kind == "float":
        return float(value) if type(value) is int else value
    if ftype.kind == "dict" and isinstance(value, dict) and ftype.item is not None:
        return {key: floated(ftype.item, item) for key, item in value.items()}
    return floated_sequence(ftype, value)


def floated_sequence(ftype: FieldType, value: Any) -> Any:
    if ftype.kind == "tuple" and isinstance(value, tuple):
        return tuple(
            floated(kind, item) for kind, item in zip(ftype.items, value, strict=False)
        )
    if (
        ftype.kind == "list"
        and ftype.item is not None
        and isinstance(value, tuple | list | set | frozenset)
    ):
        container = COLLECTION_TYPES.get(ftype.collection or "", list)
        return container(floated(ftype.item, item) for item in value)
    return value


def float_fields(cls: type[Schema]) -> tuple[tuple[str, FieldType], ...]:
    """The fields that hold floats, cached per class."""
    cached = cls.__dict__.get("__float_fields__")
    if cached is None:
        cached = tuple(
            (name, spec.ftype)
            for name, spec in cls.specs().items()
            if contains_float(spec.ftype)
        )
        cls.__float_fields__ = cached
    return cached


def normalize_floats(instance: Schema) -> None:
    """Store an ``int`` given for a ``float`` field as a ``float``, as Pydantic's
    Python mode does; the wire path already returns floats."""
    for name, ftype in float_fields(type(instance)):
        current = getattr(instance, name)
        converted = floated(ftype, current)
        if converted is not current:
            object.__setattr__(instance, name, converted)


def declared_forms(cls: type[Schema]) -> tuple[tuple[str, FieldInfo], ...]:
    """The ``str`` fields declared with ``strip``, ``lower`` or ``upper``."""
    cached = cls.__dict__.get("__declared_forms__")
    if cached is None:
        cached = tuple(
            (name, spec.ftype.info)
            for name, spec in cls.specs().items()
            if spec.ftype.kind == "str"
            and (
                spec.ftype.info.strip or spec.ftype.info.lower or spec.ftype.info.upper
            )
        )
        cls.__declared_forms__ = cached
    return cached


def prepare_normalized_values(
    instance: Schema, use_aliases: bool, path_prefix: str
) -> dict[str, str]:
    cls = type(instance)
    declared = declared_forms(cls)
    if not cls.__normalizers__ and not declared:
        return {}
    proposed: dict[str, str] = {}
    for field_name, info in declared:
        value = getattr(instance, field_name)
        if isinstance(value, str):
            proposed[field_name] = normalize_text(value, info)
    for field_name, method_name in cls.__normalizers__:
        value = proposed.get(field_name, getattr(instance, field_name))
        if value is UNSET or value is None:
            continue
        proposed[field_name] = normalized_value(instance, method_name, value)
    if not proposed:
        return {}
    validate_normalized_values(cls, proposed, use_aliases, path_prefix)
    return proposed


def commit_normalized_values(instance: Schema, proposed: dict[str, str]) -> None:
    for field_name, value in proposed.items():
        object.__setattr__(instance, field_name, value)


def queue_normalization(
    queue: list[tuple[Schema, bool, str]],
    instance: Schema,
    use_aliases: bool,
    path_prefix: str,
) -> None:
    queue.append((instance, use_aliases, path_prefix))


def defer_or_normalize(
    instance: Schema, use_aliases: bool, path_prefix: str = ""
) -> None:
    queue = _NORMALIZATION_QUEUE.get()
    if queue is None:
        normalize_constructed(
            instance, use_aliases=use_aliases, path_prefix=path_prefix
        )
    else:
        queue_normalization(queue, instance, use_aliases, path_prefix)


def apply_normalization_queue(queue: list[tuple[Schema, bool, str]]) -> None:
    """Validate every new object's proposals before committing any of them."""
    prepared = [
        (
            instance,
            prepare_normalized_values(instance, use_aliases, path_prefix),
        )
        for instance, use_aliases, path_prefix in queue
    ]
    for instance, proposed in prepared:
        commit_normalized_values(instance, proposed)


def begin_normalization_queue(
    cls: type[Schema],
) -> tuple[list[tuple[Schema, bool, str]] | None, Token[Any] | None, bool]:
    """Share one deferred finalization queue across nested Schema construction."""
    current = _NORMALIZATION_QUEUE.get()
    if current is not None:
        return current, None, False
    if not graph_has_normalizers(cls):
        return None, None, False
    queue: list[tuple[Schema, bool, str]] = []
    return queue, _NORMALIZATION_QUEUE.set(queue), True


def normalized_value(instance: Schema, method_name: str, value: str) -> str:
    try:
        result = getattr(instance, method_name)(value)
    except Invalid:
        raise TypeError(
            "field normalizers must return a string; use @field_check to reject"
        ) from None
    if inspect.isawaitable(result):
        stop_awaitable(result)
        raise TypeError("field normalizers must be synchronous")
    if type(result) is not str:
        raise TypeError("field normalizers must return a string")
    return result


def stop_awaitable(value: Any) -> None:
    """Dispose an accidental awaitable without scheduling it."""
    close = getattr(value, "close", None)
    cancel = getattr(value, "cancel", None)
    if callable(close):
        close()
    if callable(cancel):
        cancel()


def validate_normalized_values(
    cls: type[Schema],
    values: dict[str, str],
    use_aliases: bool,
    path_prefix: str,
) -> None:
    """Re-run compiled constraints only for staged normalized fields."""
    skip = sorted(set(cls.specs()) - values.keys())
    data = {
        cls.specs()[name].wire_name if use_aliases else name: value
        for name, value in values.items()
    }
    _, problems = cls.native_schema().validate(
        data,
        construct=False,
        use_aliases=use_aliases,
        skip_fields=skip,
    )
    if problems:
        raise ValidationError(
            [
                ErrorDetail(
                    f"{path_prefix}.{path}"
                    if path_prefix and path
                    else path_prefix or path,
                    code,
                    message,
                )
                for path, code, message in problems
            ]
        )


def allocate_schema_instance(cls: type[Schema], values: dict[str, Any]) -> Schema:
    """Call a custom ``__new__`` as normal class construction would."""
    allocator = cls.__new__
    if allocator is object.__new__:
        return object.__new__(cls)
    return allocator(cls, **values)


def install_generated_init(cls: type[Schema], generated_init: Any) -> None:
    """Install direct, wire-validated and model-conversion constructors."""

    def construct_validated(
        schema_cls: type[Schema], values: dict[str, Any], path_prefix: str = ""
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        if type(instance).__dict__.get(_TRACKS_SUPPLIED_FIELDS, False):
            object.__setattr__(instance, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(instance, **values)
        if hasattr(schema_cls, "__post_init__"):
            schema_cls._validate_instance(
                instance, use_aliases=True, path_prefix=path_prefix
            )
        else:
            schema_cls._validate_instance(
                instance,
                skip_fields=set(values),
                use_aliases=True,
                path_prefix=path_prefix,
            )
        defer_or_normalize(instance, use_aliases=True, path_prefix=path_prefix)
        return instance

    def construct_from_model(
        schema_cls: type[Schema], values: dict[str, Any]
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        if type(instance).__dict__.get(_TRACKS_SUPPLIED_FIELDS, False):
            object.__setattr__(instance, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(instance, **values)
        if not uses_generated_init(schema_cls):
            validate_constructed(instance, from_model=True)
        return instance

    @functools.wraps(generated_init)
    def tracked_init(self: Schema, **values: Any) -> None:
        if type(self) is cls and type(self).__dict__.get(
            _TRACKS_SUPPLIED_FIELDS, False
        ):
            object.__setattr__(self, _SUPPLIED_FIELDS, frozenset(values))
        generated_init(self, **values)
        if type(self) is cls:
            validate_constructed(self)

    setattr(cls, _TRUSTED_CONSTRUCTOR, classmethod(construct_validated))
    setattr(cls, _MODEL_CONSTRUCTOR, classmethod(construct_from_model))
    cls.__init__ = cast(  # type: ignore[method-assign]
        Any, core.NativeInit(tracked_init, lambda: native_init_parts(cls))
    )


def native_init_parts(cls: type[Schema]) -> core.CompiledSchema | None:
    """The compiled schema the native constructor of ``cls`` runs on, or
    ``None`` when the class keeps the Python constructor (a hook, a check, a
    normalizer, a custom initialisation, a field outside the native subset).
    Raises while the class cannot be resolved yet, so that the next
    construction asks again."""
    if (
        cls.is_generic()
        or not direct_construction(cls)
        or typed_decoder(cls) is None
        or not all(field.init for field in dataclasses.fields(cast(Any, cls)))
    ):
        return None
    return cls.native_schema()


def install_custom_init(cls: type[Schema], original_init: Any) -> None:
    """Install validated construction around an application-defined init."""

    def construct_validated(
        schema_cls: type[Schema], values: dict[str, Any], path_prefix: str = ""
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        original_init(instance, **values)
        schema_cls._validate_instance(
            instance, use_aliases=True, path_prefix=path_prefix
        )
        defer_or_normalize(instance, use_aliases=True, path_prefix=path_prefix)
        return instance

    def construct_from_model(
        schema_cls: type[Schema], values: dict[str, Any]
    ) -> Schema:
        instance = allocate_schema_instance(schema_cls, values)
        original_init(instance, **values)
        validate_constructed(instance, from_model=True)
        return instance

    @functools.wraps(original_init)
    def validated_init(self: Schema, *args: Any, **values: Any) -> None:
        original_init(self, *args, **values)
        if type(self) is cls:
            validate_constructed(self)

    setattr(cls, _TRUSTED_CONSTRUCTOR, classmethod(construct_validated))
    setattr(cls, _MODEL_CONSTRUCTOR, classmethod(construct_from_model))
    cls.__init__ = cast(Any, validated_init)  # type: ignore[method-assign]


_SLOT_STASH = "__hinoki_slot_stash__"


def _is_class_var(annotation: object) -> bool:
    if isinstance(annotation, typing.ForwardRef):
        annotation = annotation.__forward_arg__
    if isinstance(annotation, str):
        head = annotation.replace(" ", "").split("[", 1)[0]
        return head.rsplit(".", 1)[-1] == "ClassVar"
    return annotation is ClassVar or typing.get_origin(annotation) is ClassVar


def _declared_annotations(namespace: dict[str, Any]) -> dict[str, Any]:
    declared = namespace.get("__annotations__")
    if declared is not None:
        return cast(dict[str, Any], declared)
    annotate = namespace.get("__annotate_func__") or namespace.get("__annotate__")
    if annotate is None:
        return {}
    import annotationlib  # type: ignore[import-not-found,unused-ignore]  # noqa: PLC0415 - Python 3.14 only, the lazy annotations

    return cast(
        dict[str, Any],
        annotationlib.call_annotate_function(annotate, annotationlib.Format.FORWARDREF),
    )


def _slot_names(namespace: dict[str, Any], bases: tuple[type, ...]) -> list[str]:
    """The fields a new schema class declares that can live in a slot: plain
    names no base class already defines."""
    return [
        name
        for name, annotation in _declared_annotations(namespace).items()
        if not name.startswith("_")
        and not _is_class_var(annotation)
        and not any(hasattr(base, name) for base in bases)
    ]


class SchemaMeta(type):
    """Gives each schema class a slot per declared field.

    Rust reads a JSON body straight into those slots. The class body is
    unchanged for the user: a default is put back in the class while the
    schema is being built, then the slot takes its place, as for a
    ``dataclass(slots=True)`` class (which cannot be used, since it creates a
    second class)."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        slots: bool = True,
        model: object = _NO_MODEL,
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,
    ) -> SchemaMeta:
        if (
            slots
            and "__slots__" not in namespace
            and any(isinstance(base, SchemaMeta) for base in bases)
        ):
            names = _slot_names(namespace, bases)
            if names:
                namespace = dict(namespace)
                namespace[_SLOT_STASH] = {
                    field: namespace.pop(field) for field in names if field in namespace
                }
                namespace["__slots__"] = tuple(names)
        options: dict[str, object] = {}
        if model is not _NO_MODEL:
            options["model"] = model
        if config is not _CONFIG_UNSET:
            options["config"] = config
        try:
            return super().__new__(mcs, name, bases, namespace, **options)
        except TypeError as exc:
            if "instance lay-out conflict" not in str(exc):
                raise
            raise RegistrationError(
                f"{name}: more than one base schema declares fields; declare "
                "all but one with `class Base(Schema, slots=False)`"
            ) from exc

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        slots: bool = True,  # noqa: ARG002 - read by `__new__`
        model: object = _NO_MODEL,  # noqa: ARG002
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,  # noqa: ARG002
    ) -> None:
        super().__init__(name, bases, namespace)


class _SlotsHeld:
    """Takes the slot members out of a class being built and puts them back."""

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.members: dict[str, Any] = {}

    def __enter__(self) -> None:
        cls = self.cls
        stash = cls.__dict__.get(_SLOT_STASH)
        if stash is None:
            return
        for name in cls.__dict__["__slots__"]:
            self.members[name] = cls.__dict__[name]
            delattr(cls, name)
            if name in stash:
                setattr(cls, name, stash[name])
        delattr(cls, _SLOT_STASH)

    def __exit__(self, *_: object) -> None:
        for name, member in self.members.items():
            type.__setattr__(self.cls, name, member)


def _no_typed_json(_raw: bytes) -> None:
    return None


class _Cached(staticmethod):  # type: ignore[type-arg]
    """A per-class entry that ``_TypedJson`` and ``_Validate`` installed on one
    class; a subclass must not inherit it, because it belongs to that class."""


class _TypedJson:
    """``Schema.validate_json``: on first use for a class, the class gets the
    Rust decoder itself as its attribute (a builtin method, so a call costs no
    Python frame), or a function that always steps aside when a hook or check
    has to run on the ordinary path."""

    def __get__(
        self, instance: object, owner: type[Schema]
    ) -> Callable[[bytes], object | None]:
        try:
            decode = typed_decoder(owner) or _no_typed_json
        except Exception:
            return _no_typed_json
        if "validate_json" not in owner.__dict__:
            type.__setattr__(owner, "validate_json", _Cached(decode))
        return decode


class _Validate:
    """``Schema.validate``: on first use for a class with no before hook, check
    or normalizer on its graph, the class gets the Rust validator itself as its
    attribute (a builtin method: the instance is built and every problem raised
    without a Python frame). Any other class keeps the ordinary method."""

    def __get__(self, instance: object, owner: type[Schema]) -> Callable[[object], Any]:
        method = types.MethodType(Schema._validate_ordinary.__func__, owner)  # type: ignore[attr-defined]
        try:
            native = native_validator(owner)
        except Exception:
            return method
        found = method if native is None else native
        if "validate" not in owner.__dict__:
            type.__setattr__(owner, "validate", _Cached(found))
        return found


def native_validator(cls: type[Schema]) -> Callable[[object], Any] | None:
    """The Rust ``build`` of ``cls``, when nothing has to run around it."""
    if typed_decoder(cls) is None or graph_has_normalizers(cls):
        return None
    return cls.native_schema().build


def text_native(cls: type[Schema]) -> bool:
    """Whether ``validate_text`` can go straight to Rust for ``cls``: the same
    condition as the native ``validate``. Decided once per class."""
    if "__text_native__" not in cls.__dict__:
        try:
            ready = native_validator(cls) is not None
        except Exception:
            return False
        type.__setattr__(cls, "__text_native__", ready)
    return bool(cls.__dict__["__text_native__"])


def native_replace(instance: Schema, changes: dict[str, Any]) -> Schema | None:
    """The copy Rust builds for ``replace``, or ``None`` to use the Python path."""
    try:
        found = type(instance).native_schema().replace(instance, changes)
    except AttributeError:
        return None
    return cast("Schema | None", found)


def reset_inherited_entries(cls: type[Schema]) -> None:
    """Give ``cls`` its own lazy entries, so that it never inherits the entry a
    parent installs for itself later (a user's own ``validate`` is left alone)."""
    for name, fresh in (("validate_json", _TYPED_JSON), ("validate", _VALIDATE)):
        if name not in cls.__dict__ and isinstance(
            inspect.getattr_static(cls, name), _Cached | _TypedJson | _Validate
        ):
            type.__setattr__(cls, name, fresh)


_TYPED_JSON = _TypedJson()
_VALIDATE = _Validate()


if typing.TYPE_CHECKING:
    # A custom metaclass makes the type checkers stop validating class keywords
    # (`config=`, `model=`), so they see the plain `type`; `SchemaMeta` is the
    # runtime metaclass.
    _SchemaMeta = type
else:
    _SchemaMeta = SchemaMeta


@dataclass_transform(
    kw_only_default=True, frozen_default=True, field_specifiers=(Field,)
)
class Schema(metaclass=_SchemaMeta):
    """Base class for schemas.

    ``class PostCreate(Schema): title: str`` becomes a frozen, keyword-only
    dataclass. ``PostCreate.validate(data)`` checks decoded JSON strictly:
    no coercion, unknown keys refused by default, and every problem reported
    with its path.
    """

    __hinoki_schema__: ClassVar[bool] = True
    __slots__ = (_SUPPLIED_FIELDS, "__dict__", "__weakref__")
    __specs__: ClassVar[dict[str, FieldSpec] | None] = None
    __assigned_field_info__: ClassVar[dict[str, FieldInfo]] = {}
    __native__: ClassVar[core.CompiledSchema | None] = None
    __native_dump__: ClassVar[bool] = True
    __checks__: ClassVar[tuple[tuple[str, tuple[str, ...], str], ...]] = ()
    __normalizers__: ClassVar[tuple[tuple[str, str], ...]] = ()
    __before__: ClassVar[tuple[str, ...]] = ()
    __declared_forms__: ClassVar[tuple[tuple[str, FieldInfo], ...] | None] = None
    __float_fields__: ClassVar[tuple[tuple[str, FieldType], ...] | None] = None
    __instance_plan__: ClassVar[tuple[tuple[str, str, FieldType, bool], ...] | None] = (
        None
    )
    __graph_normalizers__: ClassVar[bool | None] = None
    __graph_checks__: ClassVar[bool | None] = None
    __graph_before__: ClassVar[bool | None] = None
    __source__: ClassVar[dict[str, Any]] = {}
    __derivation__: ClassVar[tuple[Any, ...] | None] = None
    __derived_model__: ClassVar[type | None] = None
    __schema_config__: ClassVar[SchemaConfig] = SchemaConfig()
    __computed_specs__: ClassVar[dict[str, ComputedSpec]] = {}
    __computed_checked__: ClassVar[bool] = False

    def __init_subclass__(
        cls,
        model: object = _NO_MODEL,
        config: SchemaConfig | _ConfigOmitted = _CONFIG_UNSET,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        with _SlotsHeld(cls):
            cls._build_schema(model, config)

    @classmethod
    def _build_schema(
        cls, model: object, config: SchemaConfig | _ConfigOmitted
    ) -> None:
        resolved_config = resolve_schema_config(cls, config)
        custom_init = "__init__" in cls.__dict__
        cls.__source__ = class_source(cls)
        cls.__assigned_field_info__ = {
            name: value
            for name in inspect.get_annotations(cls)
            if isinstance((value := cls.__dict__.get(name)), FieldInfo)
        }
        plan = cls.__dict__.get("__derivation__")
        if plan is not None:
            cls.__derived_model__ = cast(type, plan[0])
            derive_from_model(cls, *plan)
        elif model is not _NO_MODEL:
            source = cast(type, model)
            cls.__derived_model__ = source
            cls.__derivation__ = (source, None, (), False)
            derive_from_model(cls, source, None, (), False)
        cls.__checks__ = collect_checks(cls)
        cls.__normalizers__ = collect_normalizers(cls)
        cls.__before__ = collect_before(cls)
        annotations = inspect.get_annotations(cls)
        validate_reserved_names(cls, annotations)
        register_computed(cls, annotations)
        cls.__schema_config__ = resolved_config
        for name in annotations:
            cls.apply_default(name)
        dataclasses.dataclass(frozen=True, kw_only=True)(cls)
        reset_inherited_entries(cls)
        setattr(cls, _TRACKS_SUPPLIED_FIELDS, not custom_init)
        if custom_init:
            install_custom_init(cls, cls.__init__)
        else:
            install_generated_init(cls, cls.__init__)
        try:
            cls.__specs__ = None if cls.is_generic() else cls.build_specs()
        except ForwardReference:
            cls.__specs__ = None  # resolved on first use (recursive schemas)
        else:
            if not cls.is_generic():
                cls.computed_fields()
                refuse_hooked_unions(cls)

    def __setstate__(self, state: object) -> None:
        # Frozen fields live in slots: restore them the way the generated
        # constructor writes them.
        attributes, slots = state if isinstance(state, tuple) else (state, None)
        for name, value in (attributes or {}).items():
            object.__setattr__(self, name, value)
        for name, value in (slots or {}).items():
            object.__setattr__(self, name, value)

    @classmethod
    def is_generic(cls) -> bool:
        """True while type parameters are still open (``Envelope``)."""
        return bool(getattr(cls, "__parameters__", ()))

    def __class_getitem__(cls, args: object) -> Any:
        if not cls.is_generic():
            raise TypeError(f"{cls.__name__} is not a generic schema")
        given = args if isinstance(args, tuple) else (args,)
        if any(free_parameters(a) for a in given):
            return super().__class_getitem__(args)  # type: ignore[misc]
        from hinoki.generics import parameterize  # noqa: PLC0415 - generics uses us

        return parameterize(cls, given)

    @classmethod
    def computed_fields(cls) -> dict[str, ComputedSpec]:
        """The read-only values this schema adds to its output, by name."""
        specs = cls.__computed_specs__
        if specs and not cls.__dict__.get("__computed_checked__"):
            check_wire_names(cls, specs, {s.wire_name for s in cls.specs().values()})
            cls.__computed_checked__ = True
        return dict(specs)

    @classmethod
    def apply_default(cls, name: str) -> None:
        """Turn a ``Field(default_factory=...)`` attribute into a dataclass default."""
        attr = cls.__dict__.get(name)
        if isinstance(attr, FieldInfo):
            if attr.constraints():
                raise RegistrationError(
                    f"{cls.__name__}.{name}: constraints in Field(...) belong in "
                    "Annotated[type, Field(...)], not in the default"
                )
            if attr.default_factory is None:
                delattr(cls, name)
            else:
                setattr(
                    cls, name, dataclasses.field(default_factory=attr.default_factory)
                )

    @classmethod
    def specs(cls) -> dict[str, FieldSpec]:
        """Field descriptions, resolving forward references on first use."""
        cached = cls.__dict__.get("__specs__")
        if cached is None:
            if cls.is_generic():
                raise RegistrationError(
                    f"{cls.__name__} is generic: parameterize it first, "
                    f"for example {cls.__name__}[int]"
                )
            cached = cls.build_specs(final=True)
            cls.__specs__ = cached
        return cached

    @classmethod
    def build_specs(cls, final: bool = False) -> dict[str, FieldSpec]:
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except NameError as exc:
            if not final:
                raise ForwardReference from exc
            raise RegistrationError(f"{cls.__name__}: {exc}") from exc
        specs: dict[str, FieldSpec] = {}
        assigned_info = cls.__dict__.get("__assigned_field_info__", {})
        derived = model_fields(cls)
        for item in dataclasses.fields(cast(Any, cls)):
            assigned = assigned_info.get(item.name)
            annotation = hints[item.name]
            if isinstance(assigned, FieldInfo) and assigned.discriminator is not None:
                annotation = Annotated[annotation, assigned]
            ftype = parse_annotation(
                annotation,
                f"{cls.__name__}.{item.name}",
                input_bounds=item.name not in derived,
            )
            alias = merge_alias(
                cls.__name__,
                item.name,
                ftype.info.alias,
                assigned.alias if isinstance(assigned, FieldInfo) else None,
            )
            required = (
                item.default is dataclasses.MISSING
                and item.default_factory is dataclasses.MISSING
            )
            specs[item.name] = FieldSpec(item.name, alias, ftype, required)
        validate_aliases(cls, specs)
        cls.validate_normalizers(specs)
        return specs

    @classmethod
    def validate_normalizers(cls, specs: dict[str, FieldSpec]) -> None:
        """Check that registered normalizers have a precise typed target."""
        by_field: dict[str, str] = {}
        for field_name, method_name in cls.__normalizers__:
            spec = specs.get(field_name)
            if spec is None:
                raise RegistrationError(
                    f"{cls.__name__}.{method_name}: unknown field {field_name!r}"
                )
            if spec.ftype.kind != "str":
                raise RegistrationError(
                    f"{cls.__name__}.{field_name}: field normalizers require str fields"
                )
            if field_name in by_field:
                raise RegistrationError(
                    f"{cls.__name__}.{field_name}: only one field normalizer is allowed"
                )
            by_field[field_name] = method_name
            validate_normalizer_method(cls, method_name)
        cls.__normalizers__ = tuple(
            (name, by_field[name]) for name in specs if name in by_field
        )

    @classmethod
    def native_schema(cls) -> core.CompiledSchema:
        """The Rust validator for this schema and everything it refers to,
        compiled on first use and cached on the class."""
        cached = cls.__dict__.get("__native__")
        if cached is None:
            cached = compile_native(cls)
            cls.__native__ = cached
            # Whether the class uses the standard dump (so Rust may serialise its
            # fields directly) or overrides it (then dump() decides).
            cls.__native_dump__ = cls.dump is Schema.dump
        return cached

    if typing.TYPE_CHECKING:

        @classmethod
        def validate_json(cls, raw: bytes) -> Self | None:
            """The instance a JSON body describes, built by Rust in one pass,
            or ``None`` when the ordinary ``decode_json`` and ``validate``
            must run on the same bytes (invalid input, or a schema with hooks
            or checks)."""
            ...

    else:
        validate_json = _TYPED_JSON
        validate = _VALIDATE

    if typing.TYPE_CHECKING:

        @classmethod
        def validate(cls, data: object) -> Self:
            """Build an instance from decoded JSON or raise ``ValidationError``
            (validated in Rust)."""
            ...

    @classmethod
    def _validate_ordinary(cls, data: object) -> Self:
        return cls._validate_prepared(prepare_root(cls, data))

    @classmethod
    def _validate_prepared(cls, data: object) -> Self:
        queue, token, owns_queue = begin_normalization_queue(cls)
        try:
            instance, problems = cls.native_schema().validate(data)
            if problems:
                raise ValidationError([ErrorDetail(*problem) for problem in problems])
            if owns_queue and queue is not None:
                if token is not None:
                    _NORMALIZATION_QUEUE.reset(token)
                    token = None
                apply_normalization_queue(queue)
            if graph_has_checks(cls) and (queue is None or owns_queue):
                found = run_checks(instance, "")
                if found:
                    raise ValidationError(found)
            return cast(Self, instance)
        finally:
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)

    @classmethod
    def validate_text(cls, data: object) -> Self:
        """Like ``validate`` for data that is all text, such as form fields
        (``{"count": ["2"]}``) or a parsed XML tree: text is first converted, in
        Rust, to the ``int``, ``float`` and ``bool`` the fields declare. Anything
        that does not convert is left for ``validate`` to refuse. Before hooks see
        the raw text (each form value is a list of strings) and run first."""
        if text_native(cls):
            return cast(Self, cls.native_schema().build_text(data))
        prepared = prepare_root(cls, data)
        return cls._validate_prepared(cls.native_schema().coerce_text(prepared))

    @classmethod
    def _validate_instance(
        cls,
        instance: Schema,
        *,
        allow_naive_datetime: bool = False,
        skip_fields: set[str] | None = None,
        use_aliases: bool = False,
        path_prefix: str = "",
    ) -> None:
        """Validate final Python values on a manually constructed instance."""
        values = schema_instance_values(
            cls, instance, use_aliases=use_aliases, path=path_prefix
        )
        _, problems = cls.native_schema().validate(
            values,
            construct=False,
            use_aliases=use_aliases,
            allow_naive_datetime=allow_naive_datetime,
            skip_fields=sorted(skip_fields or ()),
        )
        if problems:
            raise ValidationError(
                [
                    ErrorDetail(
                        f"{path_prefix}.{path}"
                        if path_prefix and path
                        else path_prefix or path,
                        code,
                        message,
                    )
                    for path, code, message in problems
                ]
            )

    @classmethod
    def fields(cls) -> dict[str, FieldSpec]:
        """Immutable description shared by binding, docs and rules."""
        return dict(cls.specs())

    def to_dict(self, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
        """The field values as Python objects (UUID, datetime stay what they are),
        nested schemas converted too. Fields left ``UNSET`` are omitted, so the
        result is exactly "what the client sent" for a partial update."""
        return dump_instance(self, False, list(exclude), False)

    def replace(self, **changes: Any) -> Self:
        """A copy with ``changes`` (Python field names) applied. Every value is
        validated again, normalised and checked like a new instance, so the copy
        is as trustworthy as any other; fields that were never supplied stay
        unsupplied for ``dump(exclude_unset=True)``."""
        found = native_replace(self, changes)
        if found is not None:
            return cast(Self, found)
        specs = self.specs()
        unknown = [name for name in changes if name not in specs]
        if unknown:
            raise TypeError(
                f"{type(self).__name__}.replace() got an unknown field {unknown[0]!r}"
            )
        supplied = getattr(self, _SUPPLIED_FIELDS, None)
        kept = {
            name: getattr(self, name)
            for name in specs
            if supplied is None or name in supplied
        }
        return type(self)(**{**kept, **changes})

    @classmethod
    def from_models(cls, rows: Iterable[object], **extra: Any) -> list[Self]:
        """Build a list from a synchronous iterable of model rows.

        Each row follows ``from_model`` exactly. The iterable is consumed once,
        eagerly; this reads already-loaded attributes and never performs I/O.
        """
        return [cls.from_model(row, **extra) for row in rows]

    @classmethod
    def from_model(cls, row: object, **extra: Any) -> Self:
        """Build this schema from a model instance: every field is read from the
        attribute of the same name, ``extra`` supplies or overrides values (for
        computed fields). Nested models become nested schemas. A field the row
        lacks is an error, and an unloaded relation still raises
        ``RelationNotLoaded``: nothing here does I/O."""
        if extra:
            unknown = set(extra) - set(cls.specs())
            if unknown:
                raise TypeError(f"{cls.__name__} has no fields {sorted(unknown)}")
        queue, token, owns_queue = begin_normalization_queue(cls)
        try:
            instance = cast(Self, model_plan(cls).build(row, extra))
            if owns_queue and queue is not None:
                set_model_normalization_paths(instance, queue)
            generated = uses_generated_init(cls)
            if generated:
                cls._validate_instance(instance, allow_naive_datetime=True)
                defer_or_normalize(instance, use_aliases=False)
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)
                token = None
            finish_model_projection(cls, instance, queue, owns_queue, generated)
            return instance
        finally:
            if token is not None:
                _NORMALIZATION_QUEUE.reset(token)

    def dump(
        self,
        *,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        exclude_unset: bool = False,
        by_alias: bool = True,
    ) -> dict[str, Any]:
        """Return JSON-compatible data with optional projection controls.

        Selectors use Python field names. ``exclude_unset`` omits defaulted
        fields not supplied at construction. HTTP responses use default options.
        """
        if (
            include is None
            and type(exclude) is tuple
            and not exclude
            and exclude_unset is False
            and by_alias is True
        ):
            return dump_instance(self, True, by_alias=True)
        if type(exclude_unset) is not bool or type(by_alias) is not bool:
            raise TypeError("exclude_unset and by_alias must be bool")
        known: dict[str, object] = (
            {**self.specs(), **self.computed_fields()}
            if include is not None or exclude
            else {}
        )
        selected_include = normalize_selector(include, "include", known)
        selected_exclude = normalize_selector(exclude, "exclude", known) or []
        if (
            selected_include is None
            and not selected_exclude
            and not exclude_unset
            and by_alias
        ):
            return dump_instance(self, True, by_alias=True)
        return core.serialize_instance(
            self,
            selected_include,
            selected_exclude,
            exclude_unset,
            by_alias,
        )


def normalize_selector(
    values: Iterable[str] | None, label: str, specs: Mapping[str, object]
) -> list[str] | None:
    """Validate one shallow Python-name field selector."""
    if values is None:
        if label == "include":
            return None
        raise TypeError(f"{label} must be an iterable of field names")
    if isinstance(values, str):
        raise TypeError(f"{label} must be an iterable of field names, not str")
    try:
        names = list(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of field names") from exc
    if any(not isinstance(name, str) for name in names):
        raise TypeError(f"{label} members must be strings")
    unknown = sorted(set(names) - specs.keys())
    if unknown:
        raise ValueError(f"unknown {label} fields: {unknown}")
    return names


def chosen_columns(
    cls: type,
    model: Any,
    fields: Iterable[str] | None,
    exclude: tuple[str, ...],
    optional: bool | Iterable[str],
) -> tuple[list[str], set[str], bool]:
    """The columns to take, the names made optional, and whether all are."""
    if fields is not None and exclude:
        raise RegistrationError(f"{cls.__name__}: use fields= or exclude=, not both")
    columns = list(model.__columns__)
    chosen = (
        list(fields) if fields is not None else [c for c in columns if c not in exclude]
    )
    unknown = [n for n in [*chosen, *exclude] if n not in columns]
    if unknown:
        raise RegistrationError(
            f"{cls.__name__}: {model.__name__} has no columns {unknown}"
        )
    some = set() if isinstance(optional, bool) else set(optional)
    stray = sorted(some - set(chosen))
    if stray:
        raise RegistrationError(
            f"{cls.__name__}: optional names unknown fields {stray}"
        )
    return chosen, some, optional is True


def collect_checks(cls: type) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    """``(kind, field names, method name)`` of every check, base classes first;
    a subclass method with the same name replaces the inherited one."""
    found: dict[str, tuple[str, tuple[str, ...], str]] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            marker = check_marker(value)
            if marker is None:
                found.pop(name, None)
                continue
            function = descriptor_function(value)
            ensure_sync_check(cls, name, function)
            found[name] = (marker[0], marker[1], name)
    return tuple(found.values())


def descriptor_function(value: Any) -> Any:
    return value.__func__ if isinstance(value, (staticmethod, classmethod)) else value


def check_marker(value: Any) -> Any:
    marker = getattr(value, CHECK_ATTR, None)
    return (
        marker
        if marker is not None
        else getattr(descriptor_function(value), CHECK_ATTR, None)
    )


def ensure_sync_check(cls: type, name: str, function: Any) -> None:
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        raise RegistrationError(
            f"{cls.__name__}.{name}: Schema checks must be synchronous"
        )


def collect_normalizers(cls: type) -> tuple[tuple[str, str], ...]:
    """Collect inherited field normalizers, honoring method overrides."""
    found: dict[str, tuple[str, str]] = {}
    for klass in reversed(cls.__mro__):
        for method_name, value in vars(klass).items():
            function = (
                value.__func__
                if isinstance(value, (staticmethod, classmethod))
                else value
            )
            field_name = getattr(value, NORMALIZE_ATTR, None)
            if field_name is None:
                field_name = getattr(function, NORMALIZE_ATTR, None)
            if field_name is not None:
                found[method_name] = (field_name, method_name)
            elif method_name in found:
                del found[method_name]
    return tuple(found.values())


def collect_before(cls: type) -> tuple[str, ...]:
    """Names of the ``@before_validate`` hooks, base classes first; a subclass
    method with the same name replaces the inherited one."""
    found: dict[str, None] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            function = descriptor_function(value)
            if getattr(function, BEFORE_ATTR, False) is True:
                ensure_sync_check(cls, name, function)
                found[name] = None
            else:
                found.pop(name, None)
    return tuple(found)


def graph_has_before(cls: type[Schema]) -> bool:
    """Whether this schema graph declares a before hook (answer cached)."""
    cached = cls.__dict__.get("__graph_before__")
    if cached is None:
        cached = reaches_before(cls)
        cls.__graph_before__ = cached
    return bool(cached)


def reaches_before(cls: type[Schema]) -> bool:
    pending = [cls]
    visited: set[type] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if current.__before__:
            return True
        pending.extend(nested_schemas(current))
    return False


def prepare_root(cls: type[Schema], data: object) -> object:
    return prepare_input(cls, data, "", 0) if graph_has_before(cls) else data


def refuse_hooked_unions(cls: type[Schema]) -> None:
    """Refuse, when ``cls`` is defined, a plain union holding a hooked schema."""
    for spec in cls.specs().values():
        pending = [spec.ftype]
        while pending:
            ftype = pending.pop()
            if ftype.kind == "anyof":
                refuse_hooked_union(ftype)
            pending.extend(ftype.children())


def prepare_input(cls: type[Schema], data: object, path: str, depth: int) -> object:
    """Apply the before hooks of ``cls`` and of the schemas nested in ``data``."""
    if depth > MAX_BEFORE_DEPTH:
        raise ValidationError(
            [ErrorDetail(path, "too_deep", "input is nested too deep")]
        )
    if not isinstance(data, dict):
        return data
    for name in cls.__before__:
        data = run_before(cls, name, data, path)
    return prepare_fields(cls, data, path, depth) if graph_has_before(cls) else data


def prepare_fields(
    cls: type[Schema], data: dict[str, Any], path: str, depth: int
) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for spec in cls.specs().values():
        if spec.wire_name in data and holds_schema(spec.ftype):
            where = f"{path}.{spec.wire_name}" if path else spec.wire_name
            value = data[spec.wire_name]
            prepared = prepare_value(spec.ftype, value, where, depth + 1)
            if prepared is not value:
                changed[spec.wire_name] = prepared
    return {**data, **changed} if changed else data


def run_before(
    cls: type[Schema], name: str, data: dict[str, Any], path: str
) -> dict[str, Any]:
    try:
        result = getattr(cls, name)(dict(data))
    except Invalid as refusal:
        raise ValidationError(
            [
                ErrorDetail(
                    joined_path(path, refusal.path), refusal.code, refusal.message
                )
            ]
        ) from None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ValidationError(
            [ErrorDetail(path, "invalid_input", "input could not be prepared")]
        ) from None
    if not isinstance(result, dict):
        raise TypeError(
            f"{cls.__name__}.{name}: a before hook must return a mapping, "
            f"not {type(result).__name__}"
        )
    return result


def joined_path(base: str, extra: str) -> str:
    return f"{base}.{extra}" if base and extra else base or extra


def prepare_value(ftype: FieldType, value: object, where: str, depth: int) -> object:
    """``value`` with the before hooks applied through lists, dicts and tuples."""
    if ftype.variants:
        return prepare_variant(ftype, value, where, depth)
    if ftype.schema is not None:
        return prepare_input(ftype.schema, value, where, depth)
    if ftype.kind == "anyof":
        refuse_hooked_union(ftype)
        return value
    return prepare_container(ftype, value, where, depth)


def prepare_container(
    ftype: FieldType, value: object, where: str, depth: int
) -> object:
    if isinstance(value, list):
        return prepare_sequence(ftype, value, where, depth)
    if isinstance(value, dict) and ftype.item is not None:
        return {
            key: prepare_value(ftype.item, item, f"{where}.{key}", depth)
            for key, item in value.items()
        }
    return value


def prepare_sequence(
    ftype: FieldType, value: list[Any], where: str, depth: int
) -> list[Any]:
    if ftype.kind == "tuple":
        children: tuple[FieldType | None, ...] = ftype.items
    else:
        children = (ftype.item,) * len(value)
    return [
        item if child is None else prepare_value(child, item, f"{where}.{index}", depth)
        for index, (child, item) in enumerate(zip(children, value, strict=False))
    ] + value[len(children) :]


def prepare_variant(ftype: FieldType, value: object, where: str, depth: int) -> object:
    """The hooks of the variant a discriminated value selects; an unknown tag is
    left for the structural validation to report."""
    if not isinstance(value, dict):
        return value
    tag = value.get(ftype.discriminator_wire_name or ftype.discriminator or "")
    for name, schema in ftype.variants:
        if name == tag:
            return prepare_input(schema, value, where, depth)
    return value


def type_reaches_before(ftype: FieldType) -> bool:
    if ftype.schema is not None and graph_has_before(ftype.schema):
        return True
    if any(graph_has_before(schema) for _, schema in ftype.variants):
        return True
    return any(type_reaches_before(child) for child in ftype.children())


def refuse_hooked_union(ftype: FieldType) -> None:
    for child in ftype.items:
        if type_reaches_before(child):
            raise RegistrationError(
                "a plain union holds a schema that uses @before_validate: "
                "declare a discriminator so the hook can be applied"
            )


def graph_has_checks(cls: type[Schema], seen: set[type] | None = None) -> bool:
    """Whether this schema graph declares a check; recursive-safe lookup."""
    cached = cls.__dict__.get("__graph_checks__")
    if cached is not None:
        return bool(cached)
    pending = [cls]
    visited: set[type] = set() if seen is None else set(seen)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        known = current.__dict__.get("__graph_checks__")
        if known is True:
            return True
        if known is False:
            continue
        if current.__checks__:
            cls.__graph_checks__ = True
            return True
        pending.extend(nested_schemas(current))
    if seen is None:
        remember_graph_without(visited, "__graph_checks__")
    return False


def remember_graph_without(visited: set[type], marker: str) -> None:
    """Cache a negative walk. Sound because a finished walk resolved every
    reachable schema's specs (which are never rebuilt) and ``__checks__`` /
    ``__normalizers__`` are fixed when a class is created, so a graph that
    held none cannot gain one; every visited class reaches only visited ones.
    A walk that started from a partial ``seen`` set proves nothing and is not
    cached."""
    for member in visited:
        setattr(member, marker, False)


def graph_has_normalizers(cls: type[Schema], seen: set[type] | None = None) -> bool:
    """Whether a schema graph needs a side-effect-free structural preflight."""
    # Recursive schema graphs make recursive memoization unsafe: a node first
    # reached through the active cycle can appear to have no normalizers even
    # though a later field in that same graph contains one. An iterative walk
    # visits each class once; a negative answer is cached only after the whole
    # walk finished (see remember_graph_without).
    cached = cls.__dict__.get("__graph_normalizers__")
    if cached is not None:
        return bool(cached)
    pending = [cls]
    visited: set[type] = set() if seen is None else set(seen)
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        known = current.__dict__.get("__graph_normalizers__")
        if known is True:
            return True
        if known is False:
            continue
        if current.__normalizers__:
            cls.__graph_normalizers__ = True
            return True
        pending.extend(nested_schemas(current))
    if seen is None:
        remember_graph_without(visited, "__graph_normalizers__")
    return False


def nested_schemas(cls: type[Schema]) -> list[type[Schema]]:
    found: list[type[Schema]] = []
    for spec in cls.specs().values():
        pending: list[FieldType] = [spec.ftype]
        while pending:
            ftype = pending.pop()
            if ftype.schema is not None:
                found.append(ftype.schema)
            found.extend(schema for _, schema in ftype.variants)
            pending.extend(ftype.children())
    return found


def run_checks(
    instance: Any,
    path: str,
    *,
    use_aliases: bool = True,
    seen: set[int] | None = None,
) -> list[ErrorDetail]:
    """Run the checks of an instance and of every schema inside it."""
    checked_instances = seen if seen is not None else set()
    identity = id(instance)
    if identity in checked_instances:
        return []
    checked_instances.add(identity)
    problems = nested_instance_checks(instance, path, use_aliases, checked_instances)
    problems.extend(declared_instance_checks(instance, path, use_aliases))
    return problems


def nested_instance_checks(
    instance: Any, path: str, use_aliases: bool, seen: set[int]
) -> list[ErrorDetail]:
    """Run checks for every Schema field nested in an instance."""
    problems: list[ErrorDetail] = []
    for name, spec in type(instance).specs().items():
        field_name = spec.wire_name if use_aliases else name
        problems.extend(
            _nested_check_problems(
                spec.ftype,
                getattr(instance, name),
                field_name,
                path,
                use_aliases=use_aliases,
                seen=seen,
            )
        )
    return problems


def declared_instance_checks(
    instance: Any, path: str, use_aliases: bool
) -> list[ErrorDetail]:
    """Run the checks declared directly or inherited by one Schema class."""
    problems: list[ErrorDetail] = []
    specs = type(instance).specs()
    for kind, names, method in type(instance).__checks__:
        for target in names if kind == "field" else (None,):
            wire_name = check_wire_name(specs, target, use_aliases)
            problems.extend(call_check(instance, method, target, wire_name, path))
    return problems


def check_wire_name(
    specs: dict[str, FieldSpec], target: str | None, use_aliases: bool
) -> str | None:
    if target is None:
        return None
    return specs[target].wire_name if use_aliases else target


def _nested_check_problems(
    ftype: FieldType,
    value: Any,
    name: str,
    path: str,
    *,
    use_aliases: bool,
    seen: set[int],
) -> list[ErrorDetail]:
    if not holds_schema(ftype):
        return []  # list[int], dict[str, str]...: nothing to check, nothing walked
    where = f"{path}.{name}" if path else name
    return value_check_problems(ftype, value, where, use_aliases, seen)


def holds_schema(ftype: FieldType) -> bool:
    """Whether a value of this type can contain a Schema instance."""
    if ftype.schema is not None or ftype.variants:
        return True
    return any(holds_schema(child) for child in ftype.children())


def value_check_problems(
    ftype: FieldType, value: Any, where: str, use_aliases: bool, seen: set[int]
) -> list[ErrorDetail]:
    """Checks of the Schema instances in one field value, through lists, dicts
    and tuples at any depth; ``ftype`` can hold a Schema."""
    if isinstance(value, Schema):
        return run_checks(value, where, use_aliases=use_aliases, seen=seen)
    return [
        problem
        for child, at, element in typed_entries(ftype, value, where)
        for problem in value_check_problems(child, element, at, use_aliases, seen)
    ]


def call_check(
    instance: Any,
    method: str,
    target: str | None,
    wire_name: str | None,
    path: str,
) -> list[ErrorDetail]:
    """One check on one field (or on the object when ``target`` is ``None``)."""
    where = f"{path}.{wire_name}" if path and wire_name else (wire_name or path)
    try:
        if target is None:
            result = getattr(instance, method)()
        else:
            value = getattr(instance, target)
            result = getattr(instance, method)(value) if value is not UNSET else None
        ensure_check_result(result)
    except Invalid as refusal:
        return [ErrorDetail(refusal.path or where, refusal.code, refusal.message)]
    return []


def ensure_check_result(result: Any) -> None:
    if result is None:
        return
    if inspect.isawaitable(result):
        stop_awaitable(result)
    raise TypeError("Schema check hooks must return None; raise Invalid to reject")


SOURCE_SKIP = frozenset(
    {
        "__dict__", "__weakref__", "__annotations__", "__annotate__", "__slots__",
        "__annotate_func__", "__annotations_cache__", "__source__",
        "__derivation__", "__derived_model__", "__model_fields__",
        _SUPPLIED_FIELDS, _TRACKS_SUPPLIED_FIELDS,
        "__computed_specs__", "__computed_checked__", COMPUTED_TABLE,
        "__parameters__", "__orig_bases__", "__type_params__", "__graph_before__",
    }
)  # fmt: skip


def free_parameters(value: object) -> tuple[Any, ...]:
    """Type variables still open in ``value``."""
    if isinstance(value, typing.TypeVar):
        return (value,)
    found = getattr(value, "__parameters__", ())
    return found if isinstance(found, tuple) else ()


def class_source(cls: type) -> dict[str, Any]:
    """The class body as written (own annotations included), so a derivation can
    build the class again with model columns added."""
    body = {k: v for k, v in cls.__dict__.items() if k not in SOURCE_SKIP}
    body["__annotations__"] = dict(inspect.get_annotations(cls))
    return body


def derive_from_model(
    cls: type,
    model: type,
    fields: Iterable[str] | None,
    exclude: tuple[str, ...],
    optional: bool | Iterable[str],
) -> None:
    """Add the model's columns to ``cls`` as annotations (declared ones win)."""
    from hinoki.orm.model import MARK, FieldMeta, Model  # noqa: PLC0415 - orm uses us

    if not (isinstance(model, type) and issubclass(model, Model)):
        raise RegistrationError(f"{cls.__name__}: source must be a Model class")
    chosen, some, everything = chosen_columns(cls, model, fields, exclude, optional)
    own = dict(inspect.get_annotations(cls))
    reject_unselected_inheritance(cls, model, set(chosen) | set(own), fields)
    hints = typing.get_type_hints(model, include_extras=True)
    by_name = {f.name: f for f in dataclasses.fields(cast(Any, model))}
    derived: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    for name in (n for n in chosen if n not in own):  # declared by hand: left alone
        item = by_name[name]
        meta = item.metadata.get(MARK, FieldMeta())
        annotation = hints[name]
        if meta.max_length is not None:
            annotation = Annotated[annotation, Field(max_length=meta.max_length)]
        if everything or name in some:
            derived[name], defaults[name] = annotation | Unset, UNSET
        else:
            derived[name] = annotation
            if item.default is not dataclasses.MISSING:
                defaults[name] = item.default
    cls.__annotations__ = {**derived, **own}
    cls.__model_fields__ = frozenset(derived)  # type: ignore[attr-defined]
    for name, value in defaults.items():
        setattr(cls, name, value)


def model_fields(cls: type) -> frozenset[str]:
    """Fields a class or its bases took from a model column: they hold stored
    values, so the default wire-input bounds of dict fields do not apply."""
    decided: set[str] = set()
    found: set[str] = set()
    for klass in cls.__mro__:
        own = set(inspect.get_annotations(klass)) - decided
        found |= own & klass.__dict__.get("__model_fields__", frozenset())
        decided |= own
    return frozenset(found)


def reject_unselected_inheritance(
    cls: type, model: Any, kept: set[str], fields: Iterable[str] | None
) -> None:
    """Fail when a base class would keep a model column the selection leaves out.

    Fields are inherited as usual, so ``only``/``exclude`` cannot remove a column
    that a base Schema already declares; redeclare it in the class body to keep
    it on purpose, or derive from a narrower base."""
    columns = model.__columns__
    for base in cls.__mro__[1:]:
        for name in inspect.get_annotations(base):
            if name in columns and name not in kept:
                where = "outside only=" if fields is not None else "in exclude="
                raise RegistrationError(
                    f"{cls.__name__}: inherited field {name!r} from "
                    f"{base.__name__} is {where}; inherited fields cannot be "
                    "removed, so derive from a narrower base or redeclare it"
                )


def node_of(
    ftype: FieldType, index_of: dict[type[Schema], int], queue: list[type[Schema]]
) -> tuple[Any, ...]:
    """A field type as the plain tuple the Rust compiler reads."""
    item = node_of(ftype.item, index_of, queue) if ftype.item is not None else None
    index = None
    if ftype.schema is not None:
        if ftype.schema not in index_of:
            index_of[ftype.schema] = len(index_of)
            queue.append(ftype.schema)
        index = index_of[ftype.schema]
    constraints = ftype.info.constraints()
    if ftype.input_max_length is not None:
        constraints = {**constraints, "input_max_length": ftype.input_max_length}
    if ftype.key is not None:
        constraints = {**constraints, "key": node_of(ftype.key, index_of, queue)}
    if ftype.enum_class is not None:
        constraints = {**constraints, "enum": ftype.enum_class}
    if ftype.collection is not None:
        constraints = {**constraints, "collection": ftype.collection}
    if ftype.kind in {"tuple", "anyof"}:
        constraints = {
            **constraints,
            "items": [node_of(child, index_of, queue) for child in ftype.items],
        }
    if ftype.kind == "union":
        constraints = {
            **constraints,
            "discriminator": ftype.discriminator,
            "discriminator_wire_name": ftype.discriminator_wire_name,
            "variants": [
                (tag, schema_index(schema, index_of, queue))
                for tag, schema in ftype.variants
            ],
        }
    return (ftype.kind, ftype.nullable, constraints, item, index)


def schema_index(
    schema: type[Schema], index_of: dict[type[Schema], int], queue: list[type[Schema]]
) -> int:
    if schema not in index_of:
        index_of[schema] = len(index_of)
        queue.append(schema)
    return index_of[schema]


def typed_decoder(cls: type[Schema]) -> Callable[[bytes], object | None] | None:
    """The one-pass Rust body decoder of ``cls``, or ``None`` when a before
    hook, a check or a custom validation hook on its graph has to run on the
    ordinary path."""
    if (
        graph_has_before(cls)
        or graph_has_checks(cls)
        or not graph_is_stock(cls)
        or not isinstance(inspect.getattr_static(cls, "validate"), _Cached | _Validate)
    ):
        return None
    return cls.native_schema().decode_typed


def graph_is_stock(cls: type[Schema]) -> bool:
    """Whether no schema on the graph of ``cls`` redefines a validation hook
    (``_validate_instance``, ``native_schema``, ``specs``): Rust builds and
    validates instances itself only then. Answer cached per class."""
    cached = cls.__dict__.get("__graph_stock__")
    if cached is None:
        pending = [cls]
        seen: set[type] = set()
        cached = True
        while pending and cached:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            cached = is_stock_validation(current)
            pending.extend(nested_schemas(current) if cached else ())
        cls.__graph_stock__ = cached  # type: ignore[attr-defined]
    return bool(cached)


def compile_native(root: type[Schema]) -> core.CompiledSchema:
    """Table of every schema reachable from ``root``, compiled by the Rust core."""
    index_of: dict[type[Schema], int] = {root: 0}
    queue = [root]
    definitions: list[Any] = []
    direct: list[tuple[int, frozenset[str] | None, dict[str, Any], dict[str, Any]]] = []
    position = 0
    while position < len(queue):
        current = queue[position]
        position += 1
        fields = [
            (name, spec.wire_name, node_of(spec.ftype, index_of, queue), spec.required)
            for name, spec in current.specs().items()
        ]
        if direct_construction(current):
            tracks = current.__dict__.get(_TRACKS_SUPPLIED_FIELDS, False)
            owned = dataclasses.fields(cast(Any, current))
            direct.append(
                (
                    len(definitions),
                    frozenset(current.specs()) if tracks else None,
                    {
                        field.name: field.default
                        for field in owned
                        if field.default is not dataclasses.MISSING
                    },
                    {
                        field.name: field.default_factory
                        for field in owned
                        if field.default_factory is not dataclasses.MISSING
                    },
                )
            )
        definitions.append(
            (
                current,
                fields,
                current.__schema_config__.extra is SchemaExtra.FORBID,
            )
        )
    return core.compile_schemas(
        definitions, 0, uuid.UUID, dt.datetime.fromisoformat, UNSET, direct
    )


def direct_construction(cls: type[Schema]) -> bool:
    """Whether Rust may allocate and fill validated instances itself: the
    generated constructor only assigns, and nothing else runs after it."""
    return (
        uses_generated_init(cls)
        and not cls.__normalizers__
        and cls.__dictoffset__ != 0
        and hasattr(cls, _TRUSTED_CONSTRUCTOR)
    )


def plain(value: Any) -> Any:
    """``value`` with nested schemas turned into dicts (Python values kept)."""
    if isinstance(value, Schema):
        return value.to_dict()
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def uses_generated_init(cls: type) -> bool:
    """True when the class has the dataclass-generated constructor and no
    ``__post_init__``: only then may ``from_model`` build instances directly
    (otherwise it calls the constructor, so custom initialisation still runs)."""
    init = vars(cls).get("__init__") or next(
        (vars(k)["__init__"] for k in cls.__mro__ if "__init__" in vars(k)), None
    )
    init = inspect.unwrap(init) if init is not None else None
    generated = getattr(
        getattr(init, "__code__", None), "co_filename", ""
    ) == "<string>" and not hasattr(cls, "__post_init__")
    allocator = inspect.getattr_static(cls, "__new__", object.__new__)
    if isinstance(allocator, staticmethod):
        allocator = allocator.__func__
    return generated and allocator is object.__new__


def finish_model_projection(
    cls: type[Schema],
    instance: Schema,
    queue: list[tuple[Schema, bool, str]] | None,
    owns_queue: bool,
    generated: bool,
) -> None:
    if owns_queue and queue is not None:
        apply_normalization_queue(queue)
    if graph_has_checks(cls) and (owns_queue or (generated and queue is None)):
        found = run_checks(instance, "", use_aliases=False)
        if found:
            raise ValidationError(found)


def set_model_normalization_paths(
    instance: Schema, queue: list[tuple[Schema, bool, str]]
) -> None:
    """Attach root-relative field paths to deferred model normalizers."""
    queued = {id(item): index for index, (item, _, _) in enumerate(queue)}
    walk_model_schema(instance, "", queue, queued, set())


def walk_model_schema(
    schema: Schema,
    path: str,
    queue: list[tuple[Schema, bool, str]],
    queued: dict[int, int],
    visited: set[int],
) -> None:
    identity = id(schema)
    if identity in visited:
        return
    visited.add(identity)
    index = queued.get(identity)
    if index is not None:
        queued_schema, aliases, _ = queue[index]
        queue[index] = (queued_schema, aliases, path)
    for name, spec in type(schema).specs().items():
        value = getattr(schema, name, _INSTANCE_MISSING)
        if value is _INSTANCE_MISSING:
            continue
        child_path = f"{path}.{name}" if path else name
        walk_model_value(spec.ftype, value, child_path, queue, queued, visited)


def walk_model_value(
    ftype: FieldType,
    value: Any,
    path: str,
    queue: list[tuple[Schema, bool, str]],
    queued: dict[int, int],
    visited: set[int],
) -> None:
    if value is None or value is UNSET:
        return
    nested = ftype.schema
    if nested is not None and isinstance(value, nested):
        walk_model_schema(value, path, queue, queued, visited)
        return
    for _, variant in ftype.variants:
        if isinstance(value, variant):
            walk_model_schema(value, path, queue, queued, visited)
            return
    for child, child_path, item in typed_entries(ftype, value, path):
        walk_model_value(child, item, child_path, queue, queued, visited)


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


def conversion_of(ftype: FieldType) -> tuple[Any, Any, bool]:
    """How a model attribute converts for this field (see ``from_model``):
    ``(schema, item conversion, is a dict)``. Lists and dicts are copied."""
    inner = (
        conversion_of(ftype.item)
        if ftype.item is not None and ftype.collection is None
        else None
    )
    return (ftype.schema, inner, ftype.kind == "dict")


def model_plan(cls: type[Schema]) -> core.ModelPlan:
    """The Rust plan of ``from_model`` for this class, built once."""
    cached = cls.__dict__.get("__model_plan__")
    if cached is None:
        cached = core.ModelPlan(
            cls,
            [
                (name, spec.required, conversion_of(spec.ftype))
                for name, spec in cls.specs().items()
            ],
            uses_generated_init(cls),
        )
        cls.__model_plan__ = cached  # type: ignore[attr-defined]
    return cast(core.ModelPlan, cached)


def from_row(ftype: FieldType, value: Any) -> Any:
    """Convert a model attribute for a schema field: nested models (and lists of
    them) become the nested schema; everything else passes through."""
    if value is None:
        return value
    if ftype.schema is not None and hasattr(type(value), "__table__"):
        return ftype.schema.from_model(value)
    if (
        ftype.item is not None
        and ftype.collection is None
        and isinstance(value, list | tuple)
    ):
        return [from_row(ftype.item, item) for item in value]
    return value


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
    SecretStr: lambda _secret: MASK,
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
