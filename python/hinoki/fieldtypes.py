"""Normalisation of field annotations into a small, closed type model."""

from __future__ import annotations

import dataclasses
import enum
import re
import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    cast,
    get_args,
    get_origin,
)
from uuid import UUID

from hinoki import core
from hinoki.errors import RegistrationError
from hinoki.fields import FieldInfo, SecretStr, Unset
from hinoki.objecttypes import generated_schema, is_object_type

if TYPE_CHECKING:
    from hinoki.schema import Schema

SCALARS: dict[type, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    UUID: "uuid",
    datetime: "datetime",
    date: "date",
    time: "time",
    timedelta: "timedelta",
    Decimal: "decimal",
    bytes: "bytes",
    IPv4Address: "ipv4",
    IPv6Address: "ipv6",
    IPv4Network: "ipv4network",
    IPv6Network: "ipv6network",
    Path: "path",
    SecretStr: "secret",
    Any: "any",
    object: "any",
}
# The classes a Model column of these kinds holds; the database stores text.
TEXT_STORED: dict[str, type] = {
    kind: cls
    for cls, kind in SCALARS.items()
    if cls in {IPv4Address, IPv6Address, IPv4Network, IPv6Network}
}

DECIMAL_DIGITS_CEILING = 1000
MAX_STEP_EXPONENT = 1000
"""Most digits a ``Decimal`` field may declare through ``max_digits``."""
MAX_DECIMAL_DIGITS = 64
"""Most digits a ``Decimal`` field accepts from wire input unless it declares
``max_digits``."""
MAX_BYTES_LENGTH = 65536
"""Most decoded bytes a ``bytes`` field accepts from wire input unless it
declares ``max_length``."""
MAX_PATH_LENGTH = 4096
"""Most characters a ``Path`` field accepts from wire input unless it declares
``max_length``."""

ALLOWED_CONSTRAINTS: dict[str, frozenset[str]] = {
    "path": frozenset({"min_length", "max_length"}),
    "secret": frozenset({"min_length", "max_length", "pattern"}),
    "decimal": frozenset(
        {"ge", "le", "gt", "lt", "max_digits", "decimal_places", "multiple_of"}
    ),
    "bytes": frozenset({"min_length", "max_length", "pattern"}),
    "str": frozenset(
        {"min_length", "max_length", "pattern", "one_of", "strip", "lower", "upper"}
    ),
    "int": frozenset({"ge", "le", "gt", "lt", "one_of", "multiple_of"}),
    "float": frozenset({"ge", "le", "gt", "lt", "one_of"}),
    "list": frozenset({"min_length", "max_length"}),
    "dict": frozenset({"min_length", "max_length"}),
}
KEY_CONSTRAINTS = frozenset({"min_length", "max_length", "pattern"})
MIN_UNION_VARIANTS = 2
MAX_UNION_VARIANTS = 16
MAX_DISCRIMINATOR_BYTES = 128
DICT_ARGUMENTS = 2
TUPLE_ARGUMENTS = 2
CONTROL_CHARACTER_LIMIT = 32
DELETE_CHARACTER = 127
MAX_MAPPING_KEY_LENGTH = 256
"""Longest ``dict`` key (in characters) accepted from wire input unless the key
type sets its own ``max_length``: keys are client-chosen text."""
MAX_MAPPING_ITEMS = 1000
"""Most entries a ``dict`` field accepts from wire input unless it declares
``max_length``."""


@dataclass(frozen=True, slots=True)
class FieldType:
    """A supported field type: a kind plus nullability and constraints."""

    kind: str
    nullable: bool = False
    allows_unset: bool = False
    info: FieldInfo = field(default_factory=FieldInfo)
    item: FieldType | None = None
    schema: type[Schema] | None = None
    discriminator: str | None = None
    discriminator_wire_name: str | None = None
    variants: tuple[tuple[str, type[Schema]], ...] = ()
    key: FieldType | None = None
    """The key type of a ``dict`` field (always ``str``); ``item`` is its value."""
    input_max_length: int | None = None
    """A framework bound applied to wire input only (``Schema.validate`` and
    request bodies) when no ``max_length`` is declared: the entries of a dict
    field, the characters of its keys. Stored rows and server-built values are
    not held to it."""
    enum_class: type[enum.Enum] | None = None
    """The ``Enum`` class of an enum field. Its kind is that of its values
    (``str`` or ``int``) and ``info.one_of`` lists them: validation checks the
    value, then the member replaces it."""
    collection: str | None = None
    """``"set"``, ``"frozenset"`` or ``"tuple"`` on a ``list`` field: the wire form
    is an array of ``item``; the validated value is built as this container."""
    items: tuple[FieldType, ...] = ()
    """The element types, by position, of a fixed ``tuple`` field (kind
    ``"tuple"``), or the variants, in order, of a plain union (kind ``"anyof"``);
    a tuple's length is its ``min_length`` and ``max_length``."""

    def children(self) -> tuple[FieldType, ...]:
        """The types of the elements (or dict values) a value of this type holds."""
        if self.kind in {"tuple", "anyof"}:
            return self.items
        return () if self.item is None else (self.item,)


def is_union(annotation: object) -> bool:
    return get_origin(annotation) in {typing.Union, types.UnionType}


def split_annotated(annotation: object, where: str = "field") -> tuple[Any, FieldInfo]:
    """Strip ``Annotated`` and merge its ``Field`` metadata."""
    merged: dict[str, object] = {}
    alias: str | None = None
    discriminator: str | None = None
    while get_origin(annotation) is Annotated:
        base, *extras = get_args(annotation)
        merged, alias, discriminator = merge_field_metadata(
            extras, merged, alias, discriminator, where
        )
        annotation = base
    return annotation, field_info_from_constraints(merged, alias, discriminator)


def merge_field_metadata(
    extras: list[object],
    merged: dict[str, object],
    alias: str | None,
    discriminator: str | None,
    where: str,
) -> tuple[dict[str, object], str | None, str | None]:
    """Combine one Annotated layer, rejecting ambiguous wire names."""
    for extra in extras:
        if not isinstance(extra, FieldInfo):
            continue
        merged = {**extra.constraints(), **extra.notes(), **merged}
        if extra.alias is not None:
            alias = merge_alias(alias, extra.alias, where)
        if extra.discriminator is not None:
            if discriminator is not None and discriminator != extra.discriminator:
                raise RegistrationError(f"{where}: conflicting discriminators")
            discriminator = extra.discriminator
    return merged, alias, discriminator


def merge_alias(current: str | None, candidate: str, where: str) -> str:
    """Merge aliases from nested annotations if they describe one wire name."""
    if current is not None and current != candidate:
        raise RegistrationError(
            f"{where}: conflicting aliases {current!r} and {candidate!r}"
        )
    return candidate


def field_info_from_constraints(
    constraints: dict[str, object],
    alias: str | None,
    discriminator: str | None = None,
) -> FieldInfo:
    """Build typed metadata after precedence has been resolved."""
    description = constraints.get("description")
    examples = constraints.get("examples")
    if description is not None and not (
        isinstance(description, str) and description.strip()
    ):
        raise RegistrationError("Field(description=...) must be non-empty text")
    if examples is not None and not (isinstance(examples, tuple) and examples):
        raise RegistrationError("Field(examples=...) must be a non-empty sequence")
    return FieldInfo(
        description=description,
        examples=examples,
        deprecated=cast(bool | None, constraints.get("deprecated")),
        min_length=cast(int | None, constraints.get("min_length")),
        max_length=cast(int | None, constraints.get("max_length")),
        pattern=cast(str | None, constraints.get("pattern")),
        ge=cast(float | Decimal | None, constraints.get("ge")),
        le=cast(float | Decimal | None, constraints.get("le")),
        gt=cast(float | Decimal | None, constraints.get("gt")),
        lt=cast(float | Decimal | None, constraints.get("lt")),
        one_of=cast(tuple[Any, ...] | None, constraints.get("one_of")),
        max_digits=cast(int | None, constraints.get("max_digits")),
        decimal_places=cast(int | None, constraints.get("decimal_places")),
        multiple_of=cast(int | Decimal | None, constraints.get("multiple_of")),
        strip=cast(bool | None, constraints.get("strip")),
        lower=cast(bool | None, constraints.get("lower")),
        upper=cast(bool | None, constraints.get("upper")),
        alias=alias,
        discriminator=discriminator,
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


def decimal_limits(info: FieldInfo) -> tuple[int, int | None, int | None]:
    """``(max_digits, decimal_places, max_whole_digits)`` in force on wire input;
    the whole-digits bound exists only when ``decimal_places`` is declared."""
    most = info.max_digits if info.max_digits is not None else MAX_DECIMAL_DIGITS
    places = info.decimal_places
    return most, places, None if places is None else most - places


def is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_limits(info: FieldInfo, where: str, kind: str = "") -> None:
    for name in ("min_length", "max_length"):
        value = getattr(info, name)
        if value is not None and (not is_whole(value) or abs(value) > 2**62):
            raise RegistrationError(f"{where}: {name} must be an integer")
    for name in ("ge", "le", "gt", "lt"):
        check_bound(name, getattr(info, name), kind, where)
    if kind == "decimal":
        check_decimal_digits(info, where)
    if info.multiple_of is not None and kind in {"int", "float", "decimal"}:
        check_multiple_of(info.multiple_of, kind, where)


def check_multiple_of(step: object, kind: str, where: str) -> None:
    """A positive ``int`` step; a ``Decimal`` field also takes a positive finite
    ``Decimal``. Floats have no exact multiples, so ``float`` fields refuse it."""
    if kind == "float":
        raise RegistrationError(
            f"{where}: multiple_of does not apply to float (a float multiple is "
            "inexact); use a Decimal field"
        )
    exact = is_whole(step) or (
        kind == "decimal" and isinstance(step, Decimal) and step.is_finite()
    )
    if not exact or cast(int | Decimal, step) <= 0:
        wanted = "int or Decimal" if kind == "decimal" else "int"
        raise RegistrationError(f"{where}: multiple_of must be a positive {wanted}")
    if isinstance(step, Decimal) and abs(step.adjusted()) > MAX_STEP_EXPONENT:
        raise RegistrationError(
            f"{where}: multiple_of has an exponent beyond {MAX_STEP_EXPONENT}"
        )


def check_bound(name: str, value: object, kind: str, where: str) -> None:
    if value is None:
        return
    if kind != "decimal":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RegistrationError(f"{where}: {name} must be a number")
    elif not is_whole(value) and not (isinstance(value, Decimal) and value.is_finite()):
        raise RegistrationError(f"{where}: {name} must be an int or a finite Decimal")


def check_decimal_digits(info: FieldInfo, where: str) -> None:
    most, places = info.max_digits, info.decimal_places
    if most is not None and (
        not is_whole(most) or not 1 <= most <= DECIMAL_DIGITS_CEILING
    ):
        raise RegistrationError(
            f"{where}: max_digits must be an integer from 1 to {DECIMAL_DIGITS_CEILING}"
        )
    if places is not None and (not is_whole(places) or places < 0):
        raise RegistrationError(f"{where}: decimal_places must be an integer")
    bound = most if most is not None else MAX_DECIMAL_DIGITS
    if places is not None and places > bound:
        raise RegistrationError(
            f"{where}: decimal_places {places} exceeds max_digits {bound}"
        )


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


def parse_annotation(
    annotation: object, where: str, *, input_bounds: bool = True
) -> FieldType:
    """Turn an annotation into a ``FieldType`` or fail with a clear error.
    ``input_bounds=False`` (a field derived from a model column) leaves out the
    default wire-input bounds of dict fields."""
    base, info = split_annotated(annotation, where)
    nullable = allows_unset = False
    if is_union(base):
        members = list(get_args(base))
        nullable = type(None) in members
        allows_unset = Unset in members
        rest = [m for m in members if m is not type(None) and m is not Unset]
        if len(rest) > 1:
            return build_union(rest, info, nullable, allows_unset, where)
        if len(rest) != 1:
            raise RegistrationError(f"{where}: unsupported union {annotation!r}")
        base, info = merge_single(rest[0], info, where)
    elif info.discriminator is not None:
        raise RegistrationError(
            f"{where}: discriminator is only valid on a union of Schema variants"
        )
    return build(base, info, nullable, allows_unset, where, input_bounds)


def merge_single(member: object, info: FieldInfo, where: str) -> tuple[Any, FieldInfo]:
    """The base type and merged metadata of ``Annotated[X, ...] | None``."""
    base, inner = split_annotated(member, where)
    if info.alias is not None and inner.alias is not None and info.alias != inner.alias:
        raise RegistrationError(
            f"{where}: conflicting aliases {inner.alias!r} and {info.alias!r}"
        )
    merged = field_info_from_constraints(
        {**inner.constraints(), **inner.notes(), **info.constraints(), **info.notes()},
        alias=info.alias if info.alias is not None else inner.alias,
        discriminator=info.discriminator or inner.discriminator,
    )
    return base, merged


def build_union(
    members: list[Any],
    info: FieldInfo,
    nullable: bool,
    allows_unset: bool,
    where: str,
) -> FieldType:
    """A union of Schema classes is discriminated; any other union is plain."""
    if info.discriminator is None and not all_schemas(members):
        return build_anyof(members, info, nullable, allows_unset, where)
    return build_tagged_union(members, info, nullable, allows_unset, where)


def all_schemas(members: list[Any]) -> bool:
    """Whether every member of a union is a Schema class (these need a tag)."""
    return all(
        isinstance(member, type) and hasattr(member, "__hinoki_schema__")
        for member in members
    )


def build_anyof(
    members: list[Any],
    info: FieldInfo,
    nullable: bool,
    allows_unset: bool,
    where: str,
) -> FieldType:
    """``A | B | ...``: the first variant, in declared order, that accepts the
    value is the one used. A union of Schema classes must be discriminated."""
    check_constraints(info, "anyof", where)
    if not MIN_UNION_VARIANTS <= len(members) <= MAX_UNION_VARIANTS:
        raise RegistrationError(
            f"{where}: a union takes {MIN_UNION_VARIANTS} to {MAX_UNION_VARIANTS} "
            "variants"
        )
    variants = tuple(
        parse_annotation(member, f"{where}|{index}")
        for index, member in enumerate(members)
    )
    return FieldType("anyof", nullable, allows_unset, info, items=variants)


def build_tagged_union(
    members: list[Any],
    info: FieldInfo,
    nullable: bool,
    allows_unset: bool,
    where: str,
) -> FieldType:
    """Validate the shared tag contract for an explicit Schema union."""
    from hinoki.schema import Schema  # noqa: PLC0415 - Schema imports this module

    discriminator = validate_union_metadata(info, members, where)
    variants: list[tuple[str, type[Schema]]] = []
    wire_name: str | None = None
    for member in members:
        ensure_variant_type(member, Schema, variants, where)
        schema_member = cast(type[Schema], member)
        tag, variant_wire_name = variant_discriminator(
            schema_member, discriminator, where
        )
        if wire_name is None:
            wire_name = variant_wire_name
        elif wire_name != variant_wire_name:
            raise RegistrationError(
                f"{where}: discriminator aliases must match in every variant"
            )
        if any(tag == known_tag for known_tag, _ in variants):
            raise RegistrationError(f"{where}: duplicate discriminator tag")
        variants.append((tag, schema_member))

    if wire_name is None:
        raise RegistrationError(f"{where}: union has no variants")
    return FieldType(
        "union",
        nullable,
        allows_unset,
        info,
        discriminator=discriminator,
        discriminator_wire_name=wire_name,
        variants=tuple(variants),
    )


def validate_union_metadata(info: FieldInfo, members: list[Any], where: str) -> str:
    discriminator = info.discriminator
    if discriminator is None:
        raise RegistrationError(f"{where}: union needs Field(discriminator=...)")
    if info.constraints():
        raise RegistrationError(
            f"{where}: union Field(...) accepts only discriminator=..."
        )
    if not discriminator.isidentifier():
        raise RegistrationError(f"{where}: discriminator must be a Python field name")
    if not MIN_UNION_VARIANTS <= len(members) <= MAX_UNION_VARIANTS:
        raise RegistrationError(
            f"{where}: a discriminated union needs 2 to 16 variants"
        )
    return discriminator


def ensure_variant_type(
    member: Any,
    schema_base: type[Schema],
    variants: list[tuple[str, type[Schema]]],
    where: str,
) -> None:
    if not isinstance(member, type) or not issubclass(member, schema_base):
        raise RegistrationError(
            f"{where}: discriminated union members must be Schema classes"
        )
    schema_member = member
    if any(schema_member is known for _, known in variants):
        raise RegistrationError(f"{where}: duplicate Schema union variant")
    if any(
        issubclass(schema_member, known) or issubclass(known, schema_member)
        for _, known in variants
    ):
        raise RegistrationError(
            f"{where}: Schema union variants cannot inherit from each other"
        )
    if schema_member.dump is not schema_base.dump:
        raise RegistrationError(
            f"{where}: Schema union variants cannot override dump()"
        )


def variant_discriminator(
    member: type[Schema],
    discriminator: str,
    where: str,
) -> tuple[str, str]:
    spec = member.specs().get(discriminator)
    if spec is None:
        raise RegistrationError(
            f"{where}: {member.__name__} needs discriminator field {discriminator!r}"
        )
    field_type = spec.ftype
    choices = field_type.info.one_of
    if invalid_discriminator_field(spec.required, field_type, choices):
        raise RegistrationError(
            f"{where}: {member.__name__}.{discriminator} must be a required string "
            "constrained by exactly one Field(one_of=(tag,))"
        )
    if choices is None:
        raise RegistrationError(f"{where}: discriminator tag is missing")
    return cast(str, choices[0]), spec.wire_name


def invalid_discriminator_field(
    required: bool, field_type: FieldType, choices: tuple[Any, ...] | None
) -> bool:
    return (
        field_type.kind != "str"
        or field_type.nullable
        or field_type.allows_unset
        or not required
        or choices is None
        or len(choices) != 1
        or not isinstance(choices[0], str)
        or not choices[0]
        or len(choices[0].encode("utf-8")) > MAX_DISCRIMINATOR_BYTES
        or any(
            ord(char) < CONTROL_CHARACTER_LIMIT or ord(char) == DELETE_CHARACTER
            for char in choices[0]
        )
        or set(field_type.info.constraints()) != {"one_of"}
    )


def build(
    base: Any,
    info: FieldInfo,
    nullable: bool,
    allows_unset: bool,
    where: str,
    input_bounds: bool = True,
) -> FieldType:
    item: FieldType | None = None
    schema: type[Schema] | None = None
    listed = listed_values(base, where)
    if listed is not None:
        return build_listed(listed, info, nullable, allows_unset, where)
    if base in SCALARS:
        kind = SCALARS[base]
    elif get_origin(base) is list:
        kind = "list"
        item = parse_annotation(
            get_args(base)[0], f"{where}[]", input_bounds=input_bounds
        )
    elif base is dict or get_origin(base) in {dict, set, frozenset, tuple}:
        grouped = build_grouped(base, info, where, input_bounds)
        return dataclasses.replace(
            grouped, nullable=nullable, allows_unset=allows_unset
        )
    elif isinstance(base, type) and hasattr(base, "__hinoki_schema__"):
        kind, schema = "schema", base
    elif is_object_type(base):
        kind, schema = "schema", generated_schema(base, where)
    else:
        raise RegistrationError(f"{where}: unsupported type {base!r}")
    check_constraints(info, kind, where)
    default = input_default(kind, info, input_bounds, where)
    return FieldType(
        kind,
        nullable or kind == "any",
        allows_unset,
        info,
        item,
        schema,
        input_max_length=default,
    )


INPUT_CEILINGS = {"bytes": MAX_BYTES_LENGTH, "path": MAX_PATH_LENGTH}


def input_default(
    kind: str, info: FieldInfo, input_bounds: bool, where: str
) -> int | None:
    """The framework bound on the wire length of a ``bytes`` or ``Path`` input
    (decoded bytes, or characters), when the field sets no ``max_length``."""
    ceiling = INPUT_CEILINGS.get(kind)
    if ceiling is None:
        return None
    check_reachable(info, ceiling if input_bounds else None, where)
    return ceiling if input_bounds and info.max_length is None else None


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def listed_values(
    base: Any, where: str
) -> tuple[str, tuple[Any, ...], type[enum.Enum] | None] | None:
    """The kind, values and ``Enum`` class of a ``Literal[...]`` or ``Enum``
    annotation, or ``None`` for any other type."""
    if get_origin(base) is Literal:
        values = get_args(base)
        return value_kind(values, where, "a Literal"), unique(values), None
    if isinstance(base, type) and issubclass(base, enum.Enum):
        if issubclass(base, enum.Flag):
            raise RegistrationError(f"{where}: Flag enums are not supported")
        values = tuple(member.value for member in base)
        if not values:
            raise RegistrationError(f"{where}: {base.__name__} has no members")
        return value_kind(values, where, base.__name__), unique(values), base
    return None


def unique(values: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(dict.fromkeys(values))


def value_kind(values: Sequence[Any], where: str, what: str) -> str:
    """``str`` or ``int`` for values that are all of one of the two types."""
    kinds = {value_type(value, where, what) for value in values}
    if len(kinds) > 1:
        raise RegistrationError(
            f"{where}: {what} mixes str and int values; list values of one type"
        )
    kind = kinds.pop()
    if kind == "int" and any(not INT64_MIN <= value <= INT64_MAX for value in values):
        raise RegistrationError(f"{where}: {what} has an int outside the 64-bit range")
    return kind


def value_type(value: Any, where: str, what: str) -> str:
    if value is None:
        raise RegistrationError(
            f"{where}: {what} cannot list None: declare `X | None` instead"
        )
    if isinstance(value, enum.Enum):
        raise RegistrationError(
            f"{where}: {what} cannot list enum members: use the Enum class as the type"
        )
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise RegistrationError(
            f"{where}: {what} values must be str or int, not {type(value).__name__}"
        )
    return "str" if isinstance(value, str) else "int"


def build_listed(
    listed: tuple[str, tuple[Any, ...], type[enum.Enum] | None],
    info: FieldInfo,
    nullable: bool,
    allows_unset: bool,
    where: str,
) -> FieldType:
    kind, values, enum_class = listed
    if info.constraints():
        raise RegistrationError(
            f"{where}: a Literal or Enum field lists its own values and takes no "
            "constraints"
        )
    info = dataclasses.replace(info, one_of=values)
    return FieldType(kind, nullable, allows_unset, info, enum_class=enum_class)


def normalize_text(text: str, info: FieldInfo) -> str:
    """What ``strip``, ``lower`` and ``upper`` make of ``text``."""
    if info.strip:
        text = text.strip()
    if info.lower:
        text = text.lower()
    elif info.upper:
        text = text.upper()
    return text


def check_normalisation(info: FieldInfo, where: str) -> None:
    """``lower`` and ``upper`` exclude each other, and a ``one_of`` value must
    be one the normalisation can produce."""
    if info.lower and info.upper:
        raise RegistrationError(f"{where}: lower and upper cannot both be set")
    for value in info.one_of or ():
        if isinstance(value, str) and normalize_text(value, info) != value:
            raise RegistrationError(
                f"{where}: one_of value {value!r} can never match once the "
                "field is normalised"
            )


def check_constraints(info: FieldInfo, kind: str, where: str) -> None:
    if info.pattern is not None:
        check_pattern(info.pattern, where)
    check_limits(info, where, kind)
    unsupported = set(info.constraints()) - ALLOWED_CONSTRAINTS.get(kind, frozenset())
    if unsupported:
        raise RegistrationError(
            f"{where}: constraint(s) {sorted(unsupported)} do not apply to {kind}"
        )
    check_normalisation(info, where)
    if kind in {"str", "int", "float"}:
        check_one_of(info, kind, where)


def is_hashable(ftype: FieldType) -> bool:
    """Whether the validated values of this type can be set members."""
    if ftype.kind == "list":
        return ftype.collection in {"frozenset", "tuple"} and is_hashable(
            cast(FieldType, ftype.item)
        )
    if ftype.kind == "tuple":
        return all(is_hashable(item) for item in ftype.items)
    if ftype.kind == "anyof":
        return all(is_hashable(item) for item in ftype.items)
    return ftype.kind not in {"dict", "schema", "union", "any"}


def build_grouped(
    base: Any, info: FieldInfo, where: str, input_bounds: bool
) -> FieldType:
    """A ``dict``, ``set``, ``frozenset`` or ``tuple`` annotation."""
    if base is dict or get_origin(base) is dict:
        return build_mapping(base, info, where, input_bounds)
    return build_collection(base, info, where, input_bounds)


def build_collection(
    base: Any, info: FieldInfo, where: str, input_bounds: bool
) -> FieldType:
    """``set[X]``, ``frozenset[X]``, ``tuple[X, ...]`` (an array of ``X`` built as
    that container) and ``tuple[X, Y]`` (an array of exactly those positions)."""
    origin = cast(type, get_origin(base))
    arguments = get_args(base)
    if not arguments:
        raise RegistrationError(
            f"{where}: an empty tuple type is unsupported; write tuple[X, ...] "
            "or tuple[X, Y]"
            if origin is tuple
            else f"{where}: a {origin.__name__} field needs its item type"
        )
    if Ellipsis in arguments and not (
        origin is tuple and len(arguments) == TUPLE_ARGUMENTS and arguments[1] is ...
    ):
        raise RegistrationError(
            f"{where}: Ellipsis is only valid as tuple[X, ...]; a set or a fixed "
            "tuple lists its types"
        )
    if origin is tuple and arguments[-1] is not ...:
        return build_fixed_tuple(arguments, info, where, input_bounds)
    if len(arguments) != (TUPLE_ARGUMENTS if origin is tuple else 1):
        raise RegistrationError(f"{where}: {origin.__name__} takes one item type")
    check_constraints(info, "list", where)
    item = parse_annotation(arguments[0], f"{where}[]", input_bounds=input_bounds)
    if origin is not tuple and not is_hashable(item):
        raise RegistrationError(
            f"{where}: {origin.__name__} items must be hashable (a scalar, an Enum, "
            "a frozenset or a tuple of those), not a Schema, list, dict or set"
        )
    return FieldType("list", info=info, item=item, collection=origin.__name__)


def build_fixed_tuple(
    arguments: tuple[Any, ...], info: FieldInfo, where: str, input_bounds: bool
) -> FieldType:
    if info.constraints():
        raise RegistrationError(
            f"{where}: a fixed tuple takes no constraints; its length is its types"
        )
    items = tuple(
        parse_annotation(argument, f"{where}[{index}]", input_bounds=input_bounds)
        for index, argument in enumerate(arguments)
    )
    sized = dataclasses.replace(info, min_length=len(items), max_length=len(items))
    return FieldType("tuple", info=sized, items=items)


def build_mapping(
    base: Any, info: FieldInfo, where: str, input_bounds: bool
) -> FieldType:
    """``dict[str, X]``: string keys, values of any supported type. Unless it is
    derived from a model column, wire input is held to a default entry count
    and key length (see ``MAX_MAPPING_ITEMS``)."""
    arguments = get_args(base)
    if len(arguments) != DICT_ARGUMENTS:
        raise RegistrationError(
            f"{where}: a dict field needs its key and value types: use dict[str, X]"
        )
    check_constraints(info, "dict", where)
    key = mapping_key(arguments[0], where, input_bounds)
    item = parse_annotation(arguments[1], f"{where}{{}}", input_bounds=input_bounds)
    default = MAX_MAPPING_ITEMS if input_bounds and info.max_length is None else None
    check_reachable(info, default, where)
    return FieldType("dict", info=info, item=item, key=key, input_max_length=default)


def check_reachable(info: FieldInfo, default: int | None, where: str) -> None:
    """Refuse a ``min_length`` that no value can meet under the bound in force."""
    bound = info.max_length if info.max_length is not None else default
    if info.min_length is not None and bound is not None and info.min_length > bound:
        raise RegistrationError(
            f"{where}: min_length {info.min_length} exceeds the maximum {bound}"
        )


def mapping_key(annotation: object, where: str, input_bounds: bool) -> FieldType:
    """The ``str`` key type of a dict, bounded on wire input by default."""
    base, info = split_annotated(annotation, where)
    if base is not str:
        raise RegistrationError(
            f"{where}: dict keys must be str: use dict[str, X], with "
            "Annotated[str, Field(...)] as the key type to constrain keys"
        )
    extra = set(info.constraints()) - KEY_CONSTRAINTS
    if extra or info.alias is not None or info.discriminator is not None:
        raise RegistrationError(
            f"{where}: dict keys accept only min_length, max_length and pattern"
        )
    check_constraints(info, "str", f"{where} key")
    default = (
        MAX_MAPPING_KEY_LENGTH if input_bounds and info.max_length is None else None
    )
    check_reachable(info, default, f"{where} key")
    return FieldType("str", info=info, input_max_length=default)
