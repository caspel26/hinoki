"""Schema graph native compilation, model derivation, projection, and row conversion."""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import typing
import uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Annotated, Any, cast

from hinoki import core
from hinoki.computed import COMPUTED_TABLE
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fields import UNSET, Field, Unset
from hinoki.fieldtypes import FieldType
from hinoki.schema.decorators import (
    _INSTANCE_MISSING,
    _SUPPLIED_FIELDS,
    _TRACKS_SUPPLIED_FIELDS,
    _TRUSTED_CONSTRUCTOR,
)
from hinoki.schema.hooks import (
    chosen_columns,
    graph_has_checks,
    link_id_fields,
    nested_schemas,
    run_checks,
)
from hinoki.schema.normalize import apply_normalization_queue
from hinoki.schema.validate import typed_entries
from hinoki.schemaconfig import SchemaExtra
from hinoki.schemafast import is_stock_validation

if TYPE_CHECKING:
    from hinoki.schema.base import Schema

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
    links = link_id_fields(model)
    for name in (n for n in chosen if n not in own):  # declared by hand: left alone
        if name in links:
            annotation, default = links[name], dataclasses.MISSING
        else:
            item = by_name[name]
            annotation = bounded_annotation(
                hints[name], item.metadata.get(MARK, FieldMeta())
            )
            default = item.default
        if everything or name in some:
            derived[name], defaults[name] = annotation | Unset, UNSET
        else:
            derived[name] = annotation
            if default is not dataclasses.MISSING:
                defaults[name] = default
    cls.__annotations__ = {**derived, **own}
    cls.__model_fields__ = frozenset(derived)  # type: ignore[attr-defined]
    for name, value in defaults.items():
        setattr(cls, name, value)


def bounded_annotation(annotation: Any, meta: Any) -> Any:
    """A column's annotation, carrying its ``max_length`` as a bound."""
    if meta.max_length is None:
        return annotation
    return Annotated[annotation, Field(max_length=meta.max_length)]


def with_link_ids(
    cls: type[Schema], row: object, links: Mapping[str, Any], extra: dict[str, Any]
) -> dict[str, Any]:
    """``extra`` plus each ids field or loaded target list of ``row``'s
    many-to-many relations that the schema declares."""
    specs = cls.specs()
    found: dict[str, Any] = {}
    for side in links.values():
        if side.forward and side.ids_name in specs and side.ids_name not in extra:
            found[side.ids_name] = set(getattr(row, side.ids_name))
        if side.name in specs and side.name not in extra:
            loaded = getattr(row, side.name).loaded
            item = specs[side.name].ftype.item
            if item is not None and item.schema is not None:
                found[side.name] = [item.schema.from_model(obj) for obj in loaded]
            else:
                found[side.name] = list(loaded)
    return {**extra, **found} if found else extra


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
    computed = [spec.guide_spec() for spec in root.__computed_specs__.values()]
    return core.compile_schemas(
        definitions, 0, uuid.UUID, dt.datetime.fromisoformat, UNSET, direct, computed
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
    if getattr(type(value), "__hinoki_schema__", False):
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
