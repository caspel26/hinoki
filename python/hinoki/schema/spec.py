"""Field specifications and schema declaration-time validation."""

from __future__ import annotations

import dataclasses
import inspect
import typing
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from hinoki.computed import COMPUTED_TABLE, collect_computed
from hinoki.errors import RegistrationError
from hinoki.fieldtypes import FieldType
from hinoki.schema.decorators import (
    _CONFIG_UNSET,
    _MODEL_CONSTRUCTOR,
    _SUPPLIED_FIELDS,
    _TRACKS_SUPPLIED_FIELDS,
    _TRUSTED_CONSTRUCTOR,
    NORMALIZER_PARAMETER_COUNT,
    _ConfigOmitted,
)
from hinoki.schemaconfig import SchemaConfig

if TYPE_CHECKING:
    from hinoki.schema.base import Schema


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
    """Verify that a field normalizer has signature ``(self, value: str) -> str``."""
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
    """Whether ``annotation`` (unwrapping ``Annotated``) is ``str``."""
    if typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation is str
