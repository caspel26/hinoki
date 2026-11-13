"""Schemas: typed, immutable input/output models with strict validation."""

from __future__ import annotations

import dataclasses
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Self, cast, dataclass_transform
from uuid import UUID

from hinoki.errors import RegistrationError, ValidationError
from hinoki.fields import UNSET, Field, FieldInfo, Unset
from hinoki.fieldtypes import FieldType, parse_annotation
from hinoki.validation import INVALID, Validator

__all__ = ["UNSET", "Field", "FieldSpec", "Schema", "Unset"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Introspection record of one schema field."""

    name: str
    ftype: FieldType
    required: bool


@dataclass_transform(
    kw_only_default=True, frozen_default=True, field_specifiers=(Field,)
)
class Schema:
    """Base class for schemas.

    ``class PostCreate(Schema): title: str`` becomes a frozen, keyword-only
    dataclass. ``PostCreate.validate(data)`` checks decoded JSON strictly:
    no coercion, no unknown keys, and every problem reported with its path.
    """

    __hinoki_schema__: ClassVar[bool] = True
    __specs__: ClassVar[dict[str, FieldSpec]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for name in inspect.get_annotations(cls):
            cls.apply_default(name)
        dataclasses.dataclass(frozen=True, kw_only=True)(cls)
        cls.__specs__ = cls.build_specs()

    @classmethod
    def apply_default(cls, name: str) -> None:
        """Turn a ``Field(default_factory=...)`` attribute into a dataclass default."""
        attr = cls.__dict__.get(name)
        if isinstance(attr, FieldInfo):
            if attr.default_factory is None:
                delattr(cls, name)
            else:
                setattr(
                    cls, name, dataclasses.field(default_factory=attr.default_factory)
                )

    @classmethod
    def build_specs(cls) -> dict[str, FieldSpec]:
        try:
            hints = typing.get_type_hints(cls, include_extras=True)
        except NameError as exc:
            raise RegistrationError(f"{cls.__name__}: {exc}") from exc
        specs: dict[str, FieldSpec] = {}
        for item in dataclasses.fields(cast(Any, cls)):
            ftype = parse_annotation(hints[item.name], f"{cls.__name__}.{item.name}")
            required = (
                item.default is dataclasses.MISSING
                and item.default_factory is dataclasses.MISSING
            )
            specs[item.name] = FieldSpec(item.name, ftype, required)
        return specs

    @classmethod
    def validate(cls, data: object) -> Self:
        """Build an instance from decoded JSON or raise ``ValidationError``."""
        validator = Validator()
        value = validator.schema(FieldType("schema", schema=cls), data, "")
        if value is INVALID:
            raise ValidationError(validator.errors)
        return cast(Self, value)

    @classmethod
    def fields(cls) -> dict[str, FieldSpec]:
        """Immutable description shared by binding, docs and rules."""
        return dict(cls.__specs__)

    def dump(self) -> dict[str, Any]:
        """JSON-compatible form; ``UNSET`` fields are omitted."""
        return {
            name: encode(getattr(self, name))
            for name in self.__specs__
            if getattr(self, name) is not UNSET
        }


ENCODERS: dict[type, Callable[[Any], Any]] = {UUID: str, datetime: datetime.isoformat}


def encode(value: object) -> Any:
    """JSON-compatible form of a value produced by a schema."""
    if isinstance(value, Schema):
        return value.dump()
    if isinstance(value, list):
        return [encode(v) for v in value]
    encoder = ENCODERS.get(type(value))
    return encoder(value) if encoder else value
