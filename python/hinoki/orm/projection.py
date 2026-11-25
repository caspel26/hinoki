"""Guarded Rust ``Row -> JSON`` projection with lazy Python materialization."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import types
import weakref
from collections.abc import Callable, Iterable, Iterator
from typing import Any, SupportsIndex, cast, overload

from hinoki import core
from hinoki.errors import MultipleObjectsReturned
from hinoki.orm.links import LinkSide
from hinoki.orm.model import Model
from hinoki.orm.queryset import QuerySet, compile_simple_get_sql
from hinoki.orm.relations import ALIAS_PREFIX, store
from hinoki.pagination import Page, PageParams
from hinoki.schema import Schema, SchemaMeta, uses_generated_init

ROW_KINDS = frozenset(
    {"str", "int", "float", "bool", "uuid", "datetime", "bytes", "decimal", "date"}
)
KEY_KINDS = frozenset({"int", "str", "uuid"})
SEPARATOR = "__"

type _PlanKey = tuple[tuple[str, ...], tuple[str, ...], str]
type _PlanEntry = tuple[
    core._RowPlan | None,
    tuple[type[Schema], ...],
    tuple[type[Model], ...],
    bool,
]
_PLAN_CACHE: weakref.WeakKeyDictionary[
    type[Schema],
    weakref.WeakKeyDictionary[type[Model], dict[_PlanKey, _PlanEntry]],
] = weakref.WeakKeyDictionary()


class _ProjectedList[S: Schema](list[S]):
    """A ``list[S]`` backed by owned Rust rows and pre-encoded JSON bytes.

    Compact JSON encoding reads ``_projected_handle`` directly with zero Python
    ``Model`` / ``Schema`` allocations. Any Python element access or mutation
    materializes the items once from the owned Rust rows (no second DB query)
    and drops the pre-encoded handle so mutations stay consistent.
    """

    __slots__ = ("_materialize_fn", "_projected_handle", "_schema")
    __hash__ = None

    def __init__(
        self,
        handle: core._ProjectedRows,
        schema: type[S],
        materialize: Callable[[core._ProjectedRows], list[S]],
    ) -> None:
        super().__init__()
        self._projected_handle: core._ProjectedRows | None = handle
        self._schema: type[S] = schema
        self._materialize_fn = materialize

    def _ensure(self) -> None:
        handle = self._projected_handle
        if handle is not None:
            items = self._materialize_fn(handle)
            self._projected_handle = None
            super().extend(items)

    def __len__(self) -> int:
        handle = self._projected_handle
        if handle is not None:
            return handle.len
        return super().__len__()

    def __bool__(self) -> bool:
        handle = self._projected_handle
        if handle is not None:
            return handle.len > 0
        return super().__len__() > 0

    def __iter__(self) -> Iterator[S]:
        self._ensure()
        return super().__iter__()

    def __reversed__(self) -> Iterator[S]:
        self._ensure()
        return super().__reversed__()

    @overload
    def __getitem__(self, index: SupportsIndex) -> S: ...
    @overload
    def __getitem__(self, index: slice) -> list[S]: ...
    def __getitem__(self, index: SupportsIndex | slice) -> S | list[S]:
        self._ensure()
        return super().__getitem__(index)

    def __contains__(self, item: object) -> bool:
        self._ensure()
        return super().__contains__(item)

    def __repr__(self) -> str:
        self._ensure()
        return super().__repr__()

    def __eq__(self, other: object) -> bool:
        self._ensure()
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        self._ensure()
        return super().__ne__(other)

    def __lt__(self, other: list[S]) -> bool:
        self._ensure()
        return super().__lt__(other)

    def __le__(self, other: list[S]) -> bool:
        self._ensure()
        return super().__le__(other)

    def __gt__(self, other: list[S]) -> bool:
        self._ensure()
        return super().__gt__(other)

    def __ge__(self, other: list[S]) -> bool:
        self._ensure()
        return super().__ge__(other)

    def __copy__(self) -> list[S]:
        self._ensure()
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[S]:
        self._ensure()
        return copy.deepcopy(list(self), memo)

    @overload
    def __setitem__(self, index: SupportsIndex, value: S) -> None: ...
    @overload
    def __setitem__(self, index: slice, value: Iterable[S]) -> None: ...
    def __setitem__(self, index: SupportsIndex | slice, value: S | Iterable[S]) -> None:
        self._ensure()
        super().__setitem__(index, value)  # type: ignore[index, assignment]

    def __delitem__(self, index: SupportsIndex | slice) -> None:
        self._ensure()
        super().__delitem__(index)

    def append(self, item: S) -> None:
        self._ensure()
        super().append(item)

    def extend(self, iterable: Iterable[S]) -> None:
        self._ensure()
        super().extend(iterable)

    def insert(self, index: SupportsIndex, item: S) -> None:
        self._ensure()
        super().insert(index, item)

    def pop(self, index: SupportsIndex = -1) -> S:
        self._ensure()
        return super().pop(index)

    def remove(self, value: S) -> None:
        self._ensure()
        super().remove(value)

    def clear(self) -> None:
        self._projected_handle = None
        super().clear()

    def copy(self) -> list[S]:
        self._ensure()
        return super().copy()

    def reverse(self) -> None:
        self._ensure()
        super().reverse()

    def sort(
        self, *, key: Callable[[S], Any] | None = None, reverse: bool = False
    ) -> None:
        self._ensure()
        super().sort(key=key, reverse=reverse)

    def index(
        self,
        value: S,
        start: SupportsIndex = 0,
        stop: SupportsIndex = 2**63 - 1,
    ) -> int:
        self._ensure()
        return super().index(value, start, stop)

    def count(self, value: S) -> int:
        self._ensure()
        return super().count(value)

    def __add__(self, value: list[S]) -> list[S]:  # type: ignore[override]
        self._ensure()
        return super().__add__(value)

    def __iadd__(self, value: Iterable[S]) -> _ProjectedList[S]:  # type: ignore[override]
        self._ensure()
        super().__iadd__(value)
        return self

    def __mul__(self, value: SupportsIndex) -> list[S]:
        self._ensure()
        return super().__mul__(value)

    def __rmul__(self, value: SupportsIndex) -> list[S]:
        self._ensure()
        return super().__rmul__(value)

    def __imul__(self, value: SupportsIndex) -> _ProjectedList[S]:
        self._ensure()
        super().__imul__(value)
        return self


class _ProjectedPage[S: Schema](Page[S]):
    """A ``Page[S]`` backed by owned Rust rows and pre-encoded JSON bytes.

    Compact JSON encoding reads ``_projected_handle`` directly with zero Python
    ``Model`` / ``Schema`` allocations. Accessing ``.items`` (or ``.map()`` /
    ``.dump()``) materializes the items once from the owned Rust rows without
    issuing a second database query and clears the pre-encoded handle.
    """

    __slots__ = ("_materialize_fn", "_materialized", "_projected_handle", "_schema")

    _projected_handle: core._ProjectedRows | None
    _schema: type[S]
    _materialize_fn: Callable[[core._ProjectedRows], list[S]]
    _materialized: list[S] | None

    def __init__(
        self,
        handle: core._ProjectedRows,
        schema: type[S],
        materialize: Callable[[core._ProjectedRows], list[S]],
        page: int,
        page_size: int,
        total: int,
    ) -> None:
        object.__setattr__(self, "page", page)
        object.__setattr__(self, "page_size", page_size)
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "_projected_handle", handle)
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_materialize_fn", materialize)
        object.__setattr__(self, "_materialized", None)

    @property
    def items(self) -> list[S]:
        if self._materialized is not None:
            return self._materialized
        handle = self._projected_handle
        if handle is None:
            return []
        built = self._materialize_fn(handle)
        object.__setattr__(self, "_materialized", built)
        object.__setattr__(self, "_projected_handle", None)
        return built

    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Page):
            return NotImplemented
        return (
            self.items == other.items
            and self.page == other.page
            and self.page_size == other.page_size
            and self.total == other.total
        )

    def __copy__(self) -> Page[S]:
        return Page(
            items=list(self.items),
            page=self.page,
            page_size=self.page_size,
            total=self.total,
        )

    def __deepcopy__(self, memo: dict[int, Any]) -> Page[S]:
        return Page(
            items=copy.deepcopy(self.items, memo),
            page=self.page,
            page_size=self.page_size,
            total=self.total,
        )


_ITEM_SLOTS = frozenset(
    {"_materialize_fn", "_materialized", "_projected_handle", "_schema"}
)


class _ProjectedItem[S: Schema]:
    """A single ``S`` backed by an owned Rust row and pre-encoded JSON bytes.

    Compact JSON encoding and ``validate_response=True`` read
    ``_projected_handle`` / ``_schema`` in O(1) without allocating a Python
    ``Model`` or ``Schema``. Any attribute read, method call or mutation
    materializes the ``Schema`` once from the owned Rust rows (no second DB
    query) and drops ``_projected_handle`` so mutations stay consistent.
    """

    __slots__ = ("_materialize_fn", "_materialized", "_projected_handle", "_schema")

    _projected_handle: core._ProjectedRows | None
    _schema: type[S]
    _materialize_fn: Callable[[core._ProjectedRows], S]
    _materialized: S | None

    def __init__(
        self,
        handle: core._ProjectedRows,
        schema: type[S],
        materialize: Callable[[core._ProjectedRows], S],
    ) -> None:
        object.__setattr__(self, "_projected_handle", handle)
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_materialize_fn", materialize)
        object.__setattr__(self, "_materialized", None)

    def _ensure(self) -> S:
        if self._materialized is not None:
            return self._materialized
        handle = self._projected_handle
        if handle is None:
            raise RuntimeError("projected item has no backing row")
        built = self._materialize_fn(handle)
        object.__setattr__(self, "_materialized", built)
        object.__setattr__(self, "_projected_handle", None)
        return built

    @property  # type: ignore[misc]
    def __class__(self) -> type[S]:  # type: ignore[override]
        return self._schema

    # `dataclasses` reads these from `type(item)`, which stays `_ProjectedItem` (the
    # native encoder recognises that exact type), so they are forwarded to the
    # projected Schema: `is_dataclass`, `asdict` and `replace` work as on the Schema.
    @property
    def __dataclass_fields__(self) -> dict[str, dataclasses.Field[Any]]:
        return self._schema.__dataclass_fields__  # type: ignore[attr-defined, no-any-return]

    @property
    def __dataclass_params__(self) -> Any:
        return self._schema.__dataclass_params__  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ensure(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _ITEM_SLOTS:
            object.__setattr__(self, name, value)
            return
        setattr(self._ensure(), name, value)

    def __hash__(self) -> int:
        return hash(self._ensure())

    def __eq__(self, other: object) -> bool:
        target = other._ensure() if isinstance(other, _ProjectedItem) else other
        return bool(self._ensure() == target)

    def __repr__(self) -> str:
        return repr(self._ensure())

    def __copy__(self) -> S:
        return copy.copy(self._ensure())

    def __deepcopy__(self, memo: dict[int, Any]) -> S:
        return copy.deepcopy(self._ensure(), memo)


def _plain_descriptor(owner: type, name: str) -> bool:
    descriptor = inspect.getattr_static(owner, name, None)
    return (
        descriptor is None
        or type(descriptor) is types.MemberDescriptorType
        or all(
            inspect.getattr_static(type(descriptor), slot, None) is None
            for slot in ("__get__", "__set__", "__delete__")
        )
    )


def _generated_model_init(model: type[Model]) -> bool:
    init = vars(model).get("__init__")
    code = getattr(init, "__code__", None)
    return code is not None and code.co_filename == "<string>"


def _is_plain_model(model: type[Model]) -> bool:
    return (
        type(model) is type
        and _generated_model_init(model)
        and inspect.getattr_static(model, "__new__")
        is inspect.getattr_static(object, "__new__")
        and not hasattr(model, "__post_init__")
        and not model.__json_columns__
        and inspect.getattr_static(model, "__getattribute__")
        is inspect.getattr_static(Model, "__getattribute__")
        and inspect.getattr_static(model, "__getattr__", None)
        is inspect.getattr_static(Model, "__getattr__", None)
        and inspect.getattr_static(model, "__setattr__")
        is inspect.getattr_static(Model, "__setattr__")
        and inspect.getattr_static(model, "from_row")
        is inspect.getattr_static(Model, "from_row")
        and inspect.getattr_static(model, "remember")
        is inspect.getattr_static(Model, "remember")
        and all(_plain_descriptor(model, name) for name in model.__columns__)
    )


def _plain_schema_setattr(schema: type[Schema]) -> bool:
    setter = inspect.getattr_static(schema, "__setattr__")
    if setter is inspect.getattr_static(object, "__setattr__"):
        return True
    code = getattr(setter, "__code__", None)
    params = getattr(schema, "__dataclass_params__", None)
    return bool(params and params.frozen and code and code.co_filename == "<string>")


def _is_plain_schema(schema: type[Schema]) -> bool:
    return bool(
        type(schema) is SchemaMeta
        and uses_generated_init(schema)
        and schema.dump is Schema.dump
        and getattr(schema.from_model, "__func__", None)
        is getattr(Schema.from_model, "__func__", None)
        and inspect.getattr_static(schema, "_validate_instance")
        is inspect.getattr_static(Schema, "_validate_instance")
        and inspect.getattr_static(schema, "native_schema")
        is inspect.getattr_static(Schema, "native_schema")
        and inspect.getattr_static(schema, "specs")
        is inspect.getattr_static(Schema, "specs")
        and type(schema.__checks__) is tuple
        and not schema.__checks__
        and type(schema.__normalizers__) is tuple
        and not schema.__normalizers__
        and not schema.__computed_specs__
        and inspect.getattr_static(schema, "__post_init__", None) is None
        and inspect.getattr_static(schema, "__new__")
        is inspect.getattr_static(object, "__new__")
        and inspect.getattr_static(schema, "__getattribute__")
        is inspect.getattr_static(object, "__getattribute__")
        and inspect.getattr_static(schema, "__getattr__", None) is None
        and _plain_schema_setattr(schema)
        and all(
            _plain_descriptor(schema, item.name)
            for item in dataclasses.fields(cast(Any, schema))
        )
    )


def _scalar_field_ok(
    name: str,
    field_type: Any,
    columns: set[str],
    model_types: dict[str, Any],
) -> bool:
    if name not in columns:
        return False
    model_type = model_types[name]
    return bool(
        field_type.kind in ROW_KINDS
        and field_type.kind == model_type.kind
        and field_type.enum_class is None
        and model_type.enum_class is None
        and not field_type.info.constraints()
        and not field_type.allows_unset
        and field_type.item is None
        and field_type.schema is None
        and field_type.discriminator is None
        and not field_type.variants
    )


def _nested_plan(
    query: QuerySet[Any],
    name: str,
    spec: Any,
) -> tuple[str, str, str, list[tuple[str, str, str, bool]]] | None:
    field_type = spec.ftype
    nested_schema = field_type.schema
    if (
        name not in query.loading.selected
        or nested_schema is None
        or field_type.allows_unset
        or field_type.info.constraints()
        or field_type.item is not None
        or field_type.discriminator is not None
        or field_type.variants
    ):
        return None
    relation = query.forward(name)
    related_model = relation.model
    if (
        related_model.__soft_delete__ is not None
        or not _is_plain_model(related_model)
        or not _is_plain_schema(nested_schema)
    ):
        return None
    col_indices = {col: idx for idx, col in enumerate(related_model.__columns__)}
    rel_columns = set(related_model.__columns__)
    rel_types = related_model.__field_types__
    prefix = f"{ALIAS_PREFIX}{name}{SEPARATOR}"
    sub_fields: list[tuple[str, str, str, bool]] = []
    for sub_name, sub_spec in nested_schema.specs().items():
        if not _scalar_field_ok(sub_name, sub_spec.ftype, rel_columns, rel_types):
            return None
        alias = f"{prefix}{col_indices[sub_name]}"
        sub_fields.append(
            (alias, sub_spec.wire_name, sub_spec.ftype.kind, sub_spec.ftype.nullable)
        )
    key_col = f"{prefix}{col_indices[related_model.__primary_key__]}"
    return (name, relation.column, key_col, sub_fields)


def _scalar_fields(
    schema: type[Schema], model: type[Model]
) -> list[tuple[str, str, str, bool]] | None:
    columns = set(model.__columns__)
    model_types = model.__field_types__
    out: list[tuple[str, str, str, bool]] = []
    for sub_name, sub_spec in schema.specs().items():
        if not _scalar_field_ok(sub_name, sub_spec.ftype, columns, model_types):
            return None
        out.append(
            (sub_name, sub_spec.wire_name, sub_spec.ftype.kind, sub_spec.ftype.nullable)
        )
    return out


def _ordering_pairs(model: type[Model]) -> list[tuple[str, bool]]:
    return [(col.removeprefix("-"), col.startswith("-")) for col in model.__ordering__]


def _sqlite_decimal_ordered(model: type[Model], dialect: str) -> bool:
    if dialect != "sqlite":
        return False
    types_map = model.__field_types__
    return any(types_map[col].kind == "decimal" for col, _ in _ordering_pairs(model))


def _prefetch_item_schema(spec: Any) -> type[Schema] | None:
    ftype = spec.ftype
    item = ftype.item
    if (
        ftype.kind != "list"
        or item is None
        or item.schema is None
        or ftype.allows_unset
        or ftype.info.constraints()
        or ftype.discriminator is not None
        or ftype.variants
        or item.nullable
        or item.allows_unset
        or item.info.constraints()
        or item.item is not None
        or item.discriminator is not None
        or item.variants
    ):
        return None
    return cast("type[Schema]", item.schema)


def _m2m_prefetch_plan(
    model: type[Model],
    name: str,
    item_schema: type[Schema],
    dialect: str,
) -> core.RawPrefetchPlan | None:
    side = model.__links__[name]
    if not isinstance(side, LinkSide):
        return None
    target = side.target
    if (
        not _is_plain_model(target)
        or not _is_plain_schema(item_schema)
        or _sqlite_decimal_ordered(target, dialect)
    ):
        return None
    sub_fields = _scalar_fields(item_schema, target)
    if sub_fields is None:
        return None
    order = _ordering_pairs(target)
    target_pk = target.__primary_key__
    if not any(col == target_pk for col, _ in order):
        order.append((target_pk, False))
    table_alias = side.bridge_alias(side.base_query(None))
    bridge: core.RawBridgeSpec = (
        side.bridge.__table__,
        table_alias,
        target_pk,
        side.far,
        side.near,
    )
    return (
        name,
        target.__table__,
        list(target.__columns__),
        target.__soft_delete__,
        order,
        bridge,
        f"{table_alias}.{side.near}",
        f"{table_alias}__0",
        sub_fields,
        side.max_items,
        dialect,
    )


def _reverse_fk_prefetch_plan(
    query: QuerySet[Any],
    name: str,
    item_schema: type[Schema],
    dialect: str,
) -> core.RawPrefetchPlan | None:
    relation = query.relation(name)
    if relation.forward or relation.one_to_one:
        return None
    target = relation.model
    if (
        not _is_plain_model(target)
        or not _is_plain_schema(item_schema)
        or _sqlite_decimal_ordered(target, dialect)
    ):
        return None
    sub_fields = _scalar_fields(item_schema, target)
    if sub_fields is None:
        return None
    return (
        name,
        target.__table__,
        list(target.__columns__),
        target.__soft_delete__,
        _ordering_pairs(target),
        None,
        relation.column,
        relation.column,
        sub_fields,
        None,
        dialect,
    )


def _prefetch_plan(
    query: QuerySet[Any], name: str, spec: Any, dialect: str
) -> core.RawPrefetchPlan | None:
    if name not in query.loading.prefetched:
        return None
    item_schema = _prefetch_item_schema(spec)
    if item_schema is None:
        return None
    if name in query.model.__links__:
        return _m2m_prefetch_plan(query.model, name, item_schema, dialect)
    return _reverse_fk_prefetch_plan(query, name, item_schema, dialect)


def _sql_columns(query: QuerySet[Any]) -> list[str]:
    columns = list(query.model.__columns__)
    for rel_name in query.loading.selected:
        related_model = query.forward(rel_name).model
        prefix = f"{ALIAS_PREFIX}{rel_name}{SEPARATOR}"
        columns.extend(
            f"{prefix}{idx}" for idx in range(len(related_model.__columns__))
        )
    return columns


def _compile_schema_field(
    query: QuerySet[Any],
    name: str,
    spec: Any,
    dialect: str,
    nested: list[core.RawNestedPlan],
    prefetches: list[core.RawPrefetchPlan],
) -> tuple[str, str, str, bool] | None:
    field: tuple[str, str, str, bool] | None = None
    if spec.ftype.schema is not None:
        entry = _nested_plan(query, name, spec)
        if entry is not None:
            nested.append(entry)
            field = (name, spec.wire_name, "nested", spec.ftype.nullable)
    elif spec.ftype.kind == "list" and name in query.loading.prefetched:
        pf = _prefetch_plan(query, name, spec, dialect)
        if pf is not None:
            prefetches.append(pf)
            field = (name, spec.wire_name, "prefetch", spec.ftype.nullable)
    elif _scalar_field_ok(
        name, spec.ftype, set(query.model.__columns__), query.model.__field_types__
    ):
        field = (name, spec.wire_name, spec.ftype.kind, spec.ftype.nullable)
    return field


def _plan_key_spec(
    model: type[Model], prefetches: list[core.RawPrefetchPlan]
) -> tuple[str, str] | None:
    if not prefetches:
        return None
    pk_col = model.__primary_key__
    pk_kind = model.__field_types__[pk_col].kind
    return (pk_col, pk_kind) if pk_kind in KEY_KINDS else None


def _build_projection_plan(
    query: QuerySet[Any], schema: type[Schema], dialect: str
) -> core._RowPlan | None:
    model = query.model
    if not _is_plain_model(model) or not _is_plain_schema(schema):
        return None
    fields: list[tuple[str, str, str, bool]] = []
    nested: list[core.RawNestedPlan] = []
    prefetches: list[core.RawPrefetchPlan] = []
    for name, spec in schema.specs().items():
        field = _compile_schema_field(query, name, spec, dialect, nested, prefetches)
        if field is None:
            return None
        fields.append(field)
    key_spec = _plan_key_spec(model, prefetches)
    if len(prefetches) != len(query.loading.prefetched) or (
        prefetches and key_spec is None
    ):
        return None
    return core._RowPlan(_sql_columns(query), fields, nested, prefetches, key_spec)


_STOCK_FROM_ROW = getattr(Model.from_row, "__func__", None)
_STOCK_SCHEMA_DUMP = Schema.dump
_STOCK_SCHEMA_FROM_MODEL = getattr(Schema.from_model, "__func__", None)


def _schema_hooks_stock(schema: type[Schema]) -> bool:
    return bool(
        schema.dump is _STOCK_SCHEMA_DUMP
        and getattr(schema.from_model, "__func__", None) is _STOCK_SCHEMA_FROM_MODEL
        and not schema.__checks__
        and not schema.__normalizers__
        and not schema.__computed_specs__
    )


def _schema_guards(schema: type[Schema]) -> tuple[type[Schema], ...]:
    seen: list[type[Schema]] = []
    for spec in schema.specs().values():
        candidate = spec.ftype.schema or (
            spec.ftype.item.schema if spec.ftype.item is not None else None
        )
        if candidate is not None and candidate is not schema and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _model_guards(query: QuerySet[Any]) -> tuple[type[Model], ...]:
    seen: list[type[Model]] = []
    for rel_name in query.loading.selected:
        if rel_name in query.model.__relations__:
            target = query.forward(rel_name).model
            if target is not query.model and target not in seen:
                seen.append(target)
    for pf_name in query.loading.prefetched:
        pf_target = _prefetch_target_model(query, pf_name)
        if (
            pf_target is not None
            and pf_target is not query.model
            and pf_target not in seen
        ):
            seen.append(pf_target)
    return tuple(seen)


def _all_hooks_stock(
    schema: type[Schema],
    schemas: tuple[type[Schema], ...],
    models: tuple[type[Model], ...],
) -> bool:
    return (
        _schema_hooks_stock(schema)
        and all(_schema_hooks_stock(s) for s in schemas)
        and all(
            getattr(m.from_row, "__func__", None) is _STOCK_FROM_ROW for m in models
        )
    )


def _plan_guards(
    query: QuerySet[Any], schema: type[Schema]
) -> tuple[tuple[type[Schema], ...], tuple[type[Model], ...]]:
    return _schema_guards(schema), _model_guards(query)


def _prefetch_target_model(query: QuerySet[Any], name: str) -> type[Model] | None:
    side = query.model.__links__.get(name)
    if isinstance(side, LinkSide):
        return side.target
    if name in query.model.__relations__:
        return query.relation(name).model
    return None


def compile_projection_plan(
    query: QuerySet[Any], schema: type[Schema], dialect: str = "sqlite"
) -> core._RowPlan | None:
    """Build or fetch a cached Rust ``_RowPlan`` for ``(query, schema)``, or
    ``None`` to fall back to normal Python model hydration."""
    if (
        not query.uses_stock_builders()
        or query.loading.scoped
        or getattr(query.model.from_row, "__func__", None) is not _STOCK_FROM_ROW
    ):
        return None
    cache_key: _PlanKey = (
        query.loading.selected,
        query.loading.prefetched,
        dialect,
    )
    per_schema = _PLAN_CACHE.get(schema)
    per_model = None if per_schema is None else per_schema.get(query.model)
    if per_model is not None and cache_key in per_model:
        plan, guard_schemas, guard_models, was_stock = per_model[cache_key]
        if _all_hooks_stock(schema, guard_schemas, guard_models) == was_stock:
            return plan
    guard_schemas, guard_models = _plan_guards(query, schema)
    was_stock = _all_hooks_stock(schema, guard_schemas, guard_models)
    plan = _build_projection_plan(query, schema, dialect) if was_stock else None
    if per_schema is None:
        per_schema = weakref.WeakKeyDictionary()
        _PLAN_CACHE[schema] = per_schema
    if per_model is None:
        per_model = {}
        per_schema[query.model] = per_model
    per_model[cache_key] = (plan, guard_schemas, guard_models, was_stock)
    return plan


def _attach_m2m_rows(
    instances: list[Any], model: type[Model], name: str, raw_rows: list[dict[str, Any]]
) -> None:
    side = model.__links__[name]
    if not isinstance(side, LinkSide):
        return
    target = side.target
    target_pk = target.__primary_key__
    owner_pk = model.__primary_key__
    link_col = f"{side.bridge_alias(side.base_query(None))}__0"
    groups: dict[str, list[Any]] = {}
    targets: dict[str, Any] = {}
    for row in raw_rows:
        owner_key = str(row[link_col])
        own = {col: row[col] for col in target.__columns__}
        item = target.from_row(own)
        target_key = str(getattr(item, target_pk))
        existing = targets.setdefault(target_key, item)
        groups.setdefault(owner_key, []).append(existing)
    for instance in instances:
        found = tuple(groups.get(str(getattr(instance, owner_pk)), ()))
        ids = tuple(getattr(item, target_pk) for item in found)
        store(instance, name, (found, ids))


def _attach_reverse_fk_rows(
    query: QuerySet[Any],
    instances: list[Any],
    name: str,
    raw_rows: list[dict[str, Any]],
) -> None:
    relation = query.relation(name)
    target = relation.model
    owner_pk = query.model.__primary_key__
    groups: dict[Any, list[Any]] = {}
    for row in raw_rows:
        item = target.from_row(row)
        groups.setdefault(getattr(item, relation.column), []).append(item)
    for instance in instances:
        store(instance, name, groups.get(getattr(instance, owner_pk), []))


def _materialize_rows[S: Schema](
    query: QuerySet[Any], schema: type[S], handle: core._ProjectedRows
) -> list[S]:
    instances = [query.build(row) for row in handle.rows()]
    prefetched = handle.prefetched_rows()
    if prefetched:
        model = query.model
        for name, raw_rows in prefetched.items():
            if name in model.__links__:
                _attach_m2m_rows(instances, model, name, raw_rows)
            else:
                _attach_reverse_fk_rows(query, instances, name, raw_rows)
    return [schema.from_model(inst) for inst in instances]


async def project_list[S: Schema](
    query: QuerySet[Any], schema: type[S], database: Any
) -> list[S]:
    """Execute ``query`` and project rows directly to ``schema`` in Rust when
    eligible, falling back cleanly to ``Model -> schema.from_model``."""
    plan = compile_projection_plan(query, schema, database.dialect)
    fetch_projected = getattr(database, "fetch_projected_list", None)
    if plan is not None and fetch_projected is not None:
        sql, params = query.compile(dialect=database.dialect)
        result = await fetch_projected(sql, list(params), plan)
        if isinstance(result, core._ProjectedRows):
            return _ProjectedList(
                result,
                schema,
                lambda handle: _materialize_rows(query, schema, handle),
            )
        models = await query._hydrate(result)
        return [schema.from_model(item) for item in models]
    models = await query.fetch()
    return [schema.from_model(item) for item in models]


_MODEL_GET_FALLBACK: Any = object()


def compile_model_projection_plan(
    model: type[Model], schema: type[Schema], dialect: str = "sqlite"
) -> core._RowPlan | None:
    """Build or fetch a cached Rust ``_RowPlan`` for a plain ``(model, schema)``
    lookup without allocating an intermediate ``QuerySet`` on cache hits."""
    if getattr(model.from_row, "__func__", None) is not _STOCK_FROM_ROW:
        return None
    cache_key: _PlanKey = ((), (), dialect)
    per_schema = _PLAN_CACHE.get(schema)
    per_model = None if per_schema is None else per_schema.get(model)
    if per_model is not None and cache_key in per_model:
        plan, guard_schemas, guard_models, was_stock = per_model[cache_key]
        if _all_hooks_stock(schema, guard_schemas, guard_models) == was_stock:
            return plan
    return compile_projection_plan(QuerySet(model), schema, dialect)


def _materialize_model_one[S: Schema](
    model: type[Model], schema: type[S], handle: core._ProjectedRows
) -> S:
    return schema.from_model(model.from_row(handle.rows()[0]))


def _finish_model_one[S: Schema](
    model: type[Model],
    schema: type[S],
    result: Any,
    *,
    allow_none: bool,
) -> S | None:
    if result is None:
        if allow_none:
            return None
        raise model.NotFound(f"{model.__name__} not found")
    if result == "multiple":
        raise MultipleObjectsReturned(f"{model.__name__}: several rows match")
    if isinstance(result, core._ProjectedRows):
        return cast(
            "S",
            _ProjectedItem(
                result,
                schema,
                lambda handle: _materialize_model_one(model, schema, handle),
            ),
        )
    return schema.from_model(model.from_row(result[0]))


async def project_model_one[S: Schema](
    model: type[Model],
    schema: type[S],
    conditions: dict[str, Any],
    database: Any,
    *,
    allow_none: bool,
) -> Any:
    """Fast path for ``Model.aget_as`` / ``Model.aget_or_none_as`` on simple
    column equality lookups without allocating intermediate ``QuerySet`` objects.

    Returns ``_MODEL_GET_FALLBACK`` when the query must fall back to the
    general ``QuerySet`` path.
    """
    fetch_one = getattr(database, "fetch_projected_one", None)
    plan = (
        compile_model_projection_plan(model, schema, database.dialect)
        if fetch_one is not None
        else None
    )
    if fetch_one is None or plan is None:
        return _MODEL_GET_FALLBACK
    sql = compile_simple_get_sql(model, tuple(conditions), database.dialect)
    params = [*conditions.values(), 2]
    result = await fetch_one(sql, params, plan)
    return _finish_model_one(model, schema, result, allow_none=allow_none)


async def project_one[S: Schema](
    query: QuerySet[Any],
    schema: type[S],
    database: Any,
    *,
    allow_none: bool = False,
) -> S | None:
    """Execute ``query.limit(2)`` and project the single row directly to
    ``schema`` in Rust when eligible, returning a lazy ``_ProjectedItem``."""
    plan = compile_projection_plan(query, schema, database.dialect)
    fetch_one = getattr(database, "fetch_projected_one", None)
    if plan is not None and fetch_one is not None:
        compiled = (
            query._compile_cached("select", database.dialect, 2, query.offset_by)
            if type(query) is QuerySet
            else None
        )
        sql, params = (
            compiled
            if compiled is not None
            else query.limit(2).compile(dialect=database.dialect)
        )
        result = await fetch_one(sql, list(params), plan)
        if result is None:
            if allow_none:
                return None
            raise query.model.NotFound(f"{query.model.__name__} not found")
        if result == "multiple":
            raise MultipleObjectsReturned(f"{query.model.__name__}: several rows match")
        if isinstance(result, core._ProjectedRows):
            return cast(
                "S",
                _ProjectedItem(
                    result,
                    schema,
                    lambda handle: _materialize_rows(query, schema, handle)[0],
                ),
            )
        models = await query._hydrate(result)
        return schema.from_model(models[0])
    return await _project_one_fallback(query, schema, allow_none=allow_none)


async def _project_one_fallback[S: Schema](
    query: QuerySet[Any], schema: type[S], *, allow_none: bool
) -> S | None:
    if allow_none:
        try:
            instance = await query.aget()
        except query.model.NotFound:  # NOSONAR
            return None
        return schema.from_model(instance)
    return schema.from_model(await query.aget())


async def paginate_projected[S: Schema](
    query: QuerySet[Any],
    schema: type[S],
    params: PageParams,
    database: Any,
    *,
    concurrent: bool = False,
) -> Page[S]:
    """Execute one paginated ``query`` and project rows directly to ``schema``
    in Rust when eligible, falling back cleanly to normal pagination."""
    ordered = query.stably_ordered()
    window = ordered.limit(params.page_size).offset(params.offset)
    plan = compile_projection_plan(window, schema, database.dialect)
    fetch_projected_page = getattr(database, "fetch_projected_page", None)
    if plan is not None and fetch_projected_page is not None:
        rows_sql, rows_params = window.compile(dialect=database.dialect)
        count_sql, count_params = query.derive(
            order=(), limit_to=None, offset_by=None
        ).compile("count", database.dialect)
        result = await fetch_projected_page(
            rows_sql,
            list(rows_params),
            count_sql,
            list(count_params),
            plan,
            (params.page, params.page_size),
            concurrent=concurrent,
        )
        if isinstance(result, core._ProjectedRows):
            info = result.page_info
            total = info[2] if info is not None else 0
            return _ProjectedPage(
                result,
                schema,
                lambda handle: _materialize_rows(window, schema, handle),
                params.page,
                params.page_size,
                total,
            )
        raw_rows, total = result
        models = await window._hydrate(raw_rows)
        return Page(
            [schema.from_model(item) for item in models],
            params.page,
            params.page_size,
            total,
        )
    page = await query.apaginate(params, concurrent=concurrent)
    return page.map(schema.from_model)
