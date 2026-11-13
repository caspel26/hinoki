"""Schemas: typed, immutable input/output models with strict validation."""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import typing
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    ClassVar,
    Protocol,
    Self,
    cast,
    dataclass_transform,
    runtime_checkable,
)
from uuid import UUID

from hinoki import native
from hinoki.errors import (
    ErrorDetail,
    RegistrationError,
    RelationNotLoaded,
    ValidationError,
)
from hinoki.fields import UNSET, Field, FieldInfo, Unset
from hinoki.fieldtypes import FieldType, parse_annotation

__all__ = ["UNSET", "Field", "FieldSpec", "Schema", "Unset"]


class ForwardReference(Exception):
    """Internal: an annotation names a class that is not defined yet."""


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
    __specs__: ClassVar[dict[str, FieldSpec] | None] = None
    __native__: ClassVar[native.CompiledSchema | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for name in inspect.get_annotations(cls):
            cls.apply_default(name)
        dataclasses.dataclass(frozen=True, kw_only=True)(cls)
        try:
            cls.__specs__ = cls.build_specs()
        except ForwardReference:
            cls.__specs__ = None  # resolved on first use (recursive schemas)

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
        for item in dataclasses.fields(cast(Any, cls)):
            ftype = parse_annotation(hints[item.name], f"{cls.__name__}.{item.name}")
            required = (
                item.default is dataclasses.MISSING
                and item.default_factory is dataclasses.MISSING
            )
            specs[item.name] = FieldSpec(item.name, ftype, required)
        return specs

    @classmethod
    def native_schema(cls) -> native.CompiledSchema:
        """The Rust validator for this schema and everything it refers to,
        compiled on first use and cached on the class."""
        cached = cls.__dict__.get("__native__")
        if cached is None:
            cached = compile_native(cls)
            cls.__native__ = cached
        return cached

    @classmethod
    def validate(cls, data: object) -> Self:
        """Build an instance from decoded JSON or raise ``ValidationError``
        (validated in Rust)."""
        instance, problems = cls.native_schema().validate(data)
        if problems:
            raise ValidationError([ErrorDetail(*problem) for problem in problems])
        return cast(Self, instance)

    @classmethod
    def fields(cls) -> dict[str, FieldSpec]:
        """Immutable description shared by binding, docs and rules."""
        return dict(cls.specs())

    def to_dict(self, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
        """The field values as Python objects (UUID, datetime stay what they are),
        nested schemas converted too. Fields left ``UNSET`` are omitted, so the
        result is exactly "what the client sent" for a partial update."""
        skipped = set(exclude)
        return {
            name: plain(getattr(self, name))
            for name in self.specs()
            if name not in skipped and getattr(self, name) is not UNSET
        }

    @classmethod
    def from_model(cls, row: object, **extra: Any) -> Self:
        """Build this schema from a model instance: every field is read from the
        attribute of the same name, ``extra`` supplies or overrides values (for
        computed fields). Nested models become nested schemas. A field the row
        lacks is an error, and an unloaded relation still raises
        ``RelationNotLoaded``: nothing here does I/O."""
        values: dict[str, Any] = {}
        for name, spec in cls.specs().items():
            if name in extra:
                values[name] = extra[name]
                continue
            try:
                values[name] = from_row(spec.ftype, getattr(row, name))
            except RelationNotLoaded:
                raise  # an unloaded relation is a loading mistake, not a missing field
            except AttributeError:
                if spec.required:
                    raise AttributeError(
                        f"{cls.__name__}.{name}: {type(row).__name__} has no "
                        f"attribute {name!r}; pass it as {name}=..."
                    ) from None
        unknown = set(extra) - set(cls.specs())
        if unknown:
            raise TypeError(f"{cls.__name__} has no fields {sorted(unknown)}")
        return cls(**values)

    def dump(self) -> dict[str, Any]:
        """JSON-compatible form; ``UNSET`` fields are omitted."""
        return {
            name: encode(getattr(self, name))
            for name in self.specs()
            if getattr(self, name) is not UNSET
        }


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
    return (ftype.kind, ftype.nullable, ftype.info.constraints(), item, index)


def compile_native(root: type[Schema]) -> native.CompiledSchema:
    """Table of every schema reachable from ``root``, compiled by the Rust core."""
    index_of: dict[type[Schema], int] = {root: 0}
    queue = [root]
    definitions = []
    position = 0
    while position < len(queue):
        current = queue[position]
        position += 1
        fields = [
            (name, node_of(spec.ftype, index_of, queue), spec.required)
            for name, spec in current.specs().items()
        ]
        definitions.append((current, fields))
    return native.compile_schemas(definitions, 0, uuid.UUID, dt.datetime.fromisoformat)


def plain(value: Any) -> Any:
    """``value`` with nested schemas turned into dicts (Python values kept)."""
    if isinstance(value, Schema):
        return value.to_dict()
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def from_row(ftype: FieldType, value: Any) -> Any:
    """Convert a model attribute for a schema field: nested models (and lists of
    them) become the nested schema; everything else passes through."""
    if value is None:
        return value
    if ftype.schema is not None and hasattr(type(value), "__table__"):
        return ftype.schema.from_model(value)
    if ftype.item is not None and isinstance(value, list | tuple):
        return [from_row(ftype.item, item) for item in value]
    return value


ENCODERS: dict[type, Callable[[Any], Any]] = {UUID: str, datetime: datetime.isoformat}


@runtime_checkable
class Dumpable(Protocol):
    """Anything that can render itself as JSON-compatible data."""

    def dump(self) -> dict[str, Any]: ...


def encode_items(value: list[Any] | dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return [encode(item) for item in value]


def encode(value: object) -> Any:
    """JSON-compatible form of a value produced by a handler or schema."""
    if isinstance(value, Dumpable):
        return value.dump()
    if isinstance(value, list | dict):
        return encode_items(value)
    encoder = ENCODERS.get(type(value))
    return encoder(value) if encoder else value
