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

from hinoki import native
from hinoki.errors import (
    ErrorDetail,
    RegistrationError,
    ValidationError,
)
from hinoki.fields import UNSET, Field, FieldInfo, Unset
from hinoki.fieldtypes import FieldType, parse_annotation
from hinoki.native import dump_instance

__all__ = [
    "UNSET",
    "Field",
    "FieldSpec",
    "Invalid",
    "Schema",
    "Unset",
    "field_check",
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
    __native_dump__: ClassVar[bool] = True
    __checks__: ClassVar[tuple[tuple[str, tuple[str, ...], str], ...]] = ()

    def __init_subclass__(
        cls,
        model: type | None = None,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        optional: bool | Iterable[str] = False,
        **kwargs: object,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if model is not None:
            derive_from_model(cls, model, fields, tuple(exclude), optional)
        elif fields is not None or tuple(exclude) or optional:
            raise RegistrationError(
                f"{cls.__name__}: fields/exclude/optional need model=..."
            )
        cls.__checks__ = collect_checks(cls)
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
            # Whether the class uses the standard dump (so Rust may serialise its
            # fields directly) or overrides it (then dump() decides).
            cls.__native_dump__ = cls.dump is Schema.dump
        return cached

    @classmethod
    def validate(cls, data: object) -> Self:
        """Build an instance from decoded JSON or raise ``ValidationError``
        (validated in Rust)."""
        instance, problems = cls.native_schema().validate(data)
        if problems:
            raise ValidationError([ErrorDetail(*problem) for problem in problems])
        if graph_has_checks(cls):
            found = run_checks(instance, "")
            if found:
                raise ValidationError(found)
        return cast(Self, instance)

    @classmethod
    def fields(cls) -> dict[str, FieldSpec]:
        """Immutable description shared by binding, docs and rules."""
        return dict(cls.specs())

    def to_dict(self, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
        """The field values as Python objects (UUID, datetime stay what they are),
        nested schemas converted too. Fields left ``UNSET`` are omitted, so the
        result is exactly "what the client sent" for a partial update."""
        return dump_instance(self, False, list(exclude))

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
        return cast(Self, model_plan(cls).build(row, extra))

    def dump(self) -> dict[str, Any]:
        """JSON-compatible form; ``UNSET`` fields are omitted. Built in Rust,
        like the JSON answers themselves."""
        return dump_instance(self, True)


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
            mark = getattr(value, CHECK_ATTR, None)
            if mark is not None:
                found[name] = (mark[0], mark[1], name)
            elif name in found:
                del found[name]  # overridden by something that is not a check
    return tuple(found.values())


def graph_has_checks(cls: type[Schema], seen: set[type] | None = None) -> bool:
    """Whether this schema or any schema reachable from it declares a check
    (cached on the class, so schemas without checks pay nothing)."""
    cached = cls.__dict__.get("__graph_checks__")
    if cached is not None:
        return bool(cached)
    seen = seen if seen is not None else set()
    seen.add(cls)
    answer = bool(cls.__checks__) or any(
        graph_has_checks(nested, seen)
        for nested in nested_schemas(cls)
        if nested not in seen
    )
    cls.__graph_checks__ = answer  # type: ignore[attr-defined]
    return answer


def nested_schemas(cls: type[Schema]) -> list[type[Schema]]:
    found: list[type[Schema]] = []
    for spec in cls.specs().values():
        ftype: FieldType | None = spec.ftype
        while ftype is not None:
            if ftype.schema is not None:
                found.append(ftype.schema)
            ftype = ftype.item
    return found


def run_checks(instance: Any, path: str) -> list[ErrorDetail]:
    """Run the checks of an instance and of every schema inside it."""
    problems: list[ErrorDetail] = []
    for name in type(instance).specs():
        value = getattr(instance, name)
        where = f"{path}.{name}" if path else name
        if isinstance(value, Schema):
            problems += run_checks(value, where)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Schema):
                    problems += run_checks(item, f"{where}[{index}]")
    for kind, names, method in type(instance).__checks__:
        for target in names if kind == "field" else (None,):
            problems += call_check(instance, method, target, path)
    return problems


def call_check(
    instance: Any, method: str, target: str | None, path: str
) -> list[ErrorDetail]:
    """One check on one field (or on the object when ``target`` is ``None``)."""
    where = f"{path}.{target}" if path and target else (target or path)
    try:
        if target is None:
            getattr(instance, method)()
        else:
            value = getattr(instance, target)
            if value is not UNSET:
                getattr(instance, method)(value)
    except Invalid as refusal:
        return [ErrorDetail(refusal.path or where, refusal.code, refusal.message)]
    return []


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
        raise RegistrationError(f"{cls.__name__}: model= must be a Model class")
    chosen, some, everything = chosen_columns(cls, model, fields, exclude, optional)
    hints = typing.get_type_hints(model, include_extras=True)
    by_name = {f.name: f for f in dataclasses.fields(cast(Any, model))}
    own = dict(inspect.get_annotations(cls))
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
    for name, value in defaults.items():
        setattr(cls, name, value)


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
    return native.compile_schemas(
        definitions, 0, uuid.UUID, dt.datetime.fromisoformat, UNSET
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
    return getattr(
        getattr(init, "__code__", None), "co_filename", ""
    ) == "<string>" and not hasattr(cls, "__post_init__")


def conversion_of(ftype: FieldType) -> tuple[Any, Any]:
    """How a model attribute converts for this field (see ``from_model``)."""
    inner = conversion_of(ftype.item) if ftype.item is not None else None
    return (ftype.schema, inner)


def model_plan(cls: type[Schema]) -> native.ModelPlan:
    """The Rust plan of ``from_model`` for this class, built once."""
    cached = cls.__dict__.get("__model_plan__")
    if cached is None:
        cached = native.ModelPlan(
            cls,
            [
                (name, spec.required, conversion_of(spec.ftype))
                for name, spec in cls.specs().items()
            ],
            uses_generated_init(cls),
        )
        cls.__model_plan__ = cached  # type: ignore[attr-defined]
    return cast(native.ModelPlan, cached)


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
