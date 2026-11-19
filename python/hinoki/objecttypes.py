"""``TypedDict`` and dataclass annotations as nested ``Schema`` classes.

A field annotated with one of these is validated as a nested object. The
validated value is an instance of a generated ``Schema`` with the same name and
fields, not of the original type; the generated class is built once per type.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import TYPE_CHECKING, Any, NotRequired, Required

from hinoki.errors import RegistrationError
from hinoki.fields import UNSET, Field, Unset

if TYPE_CHECKING:
    from hinoki.schema import Schema

GENERATED: dict[type, type[Schema]] = {}
BUILDING: list[type] = []


def is_object_type(base: object) -> bool:
    """Whether an annotation is a ``TypedDict`` or a dataclass class."""
    return typing.is_typeddict(base) or (
        isinstance(base, type) and dataclasses.is_dataclass(base)
    )


def generated_schema(base: type, where: str) -> type[Schema]:
    """The ``Schema`` class standing for a ``TypedDict`` or dataclass."""
    from hinoki.schema import Schema  # noqa: PLC0415 - Schema imports this module

    if (found := GENERATED.get(base)) is not None:
        return found
    if base in BUILDING:
        raise RegistrationError(f"{where}: recursive {base.__name__} is not supported")
    BUILDING.append(base)
    try:
        hints = typing.get_type_hints(base, include_extras=True)
        if typing.is_typeddict(base):
            annotations, defaults = typeddict_fields(base, hints)
        else:
            annotations, defaults = dataclass_fields(base, hints, where)
        namespace = {
            "__annotations__": annotations,
            "__module__": base.__module__,
            "__doc__": base.__doc__ if typing.is_typeddict(base) else None,
            **defaults,
        }
        generated = typing.cast(type[Schema], type(base.__name__, (Schema,), namespace))
    finally:
        BUILDING.pop()
    GENERATED[base] = generated
    return generated


def unwrap_key(hint: Any) -> Any:
    while typing.get_origin(hint) in {Required, NotRequired}:
        hint = typing.get_args(hint)[0]
    return hint


def typeddict_fields(
    base: type, hints: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Annotations and defaults: a key that may be missing is ``| Unset``.
    ``Required`` and ``NotRequired`` are read from the resolved hints, as
    ``__required_keys__`` misses them in string annotations."""
    total = getattr(base, "__total__", True)
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    for name, hint in hints.items():
        optional = key_is_optional(hint, total)
        member = unwrap_key(hint)
        if optional:
            annotations[name] = member | Unset
            defaults[name] = UNSET
        else:
            annotations[name] = member
    return annotations, defaults


def key_is_optional(hint: Any, total: bool) -> bool:
    while (origin := typing.get_origin(hint)) is typing.Annotated:
        hint = typing.get_args(hint)[0]
    if origin is NotRequired:
        return True
    return not total and origin is not Required


def dataclass_fields(
    base: type, hints: dict[str, Any], where: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    for item in dataclasses.fields(base):
        if not item.init:
            raise RegistrationError(
                f"{where}: {base.__name__}.{item.name} is not an init field, "
                "which a Schema cannot take"
            )
        annotations[item.name] = hints[item.name]
        if item.default is not dataclasses.MISSING:
            defaults[item.name] = item.default
        elif item.default_factory is not dataclasses.MISSING:
            defaults[item.name] = Field(default_factory=item.default_factory)
    return annotations, defaults
