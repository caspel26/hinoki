"""Guarded Rust ``Row -> JSON`` projection with lazy Python materialization."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import types
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, SupportsIndex, cast, overload

from hinoki import core
from hinoki.orm.model import Model
from hinoki.orm.relations import ALIAS_PREFIX
from hinoki.pagination import Page, PageParams
from hinoki.schema import Schema, SchemaMeta, uses_generated_init

if TYPE_CHECKING:
    from hinoki.orm.queryset import QuerySet

ROW_KINDS = frozenset(
    {"str", "int", "float", "bool", "uuid", "datetime", "bytes", "decimal", "date"}
)
SEPARATOR = "__"


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


def _sql_columns(query: QuerySet[Any]) -> list[str]:
    columns = list(query.model.__columns__)
    for rel_name in query.loading.selected:
        related_model = query.forward(rel_name).model
        prefix = f"{ALIAS_PREFIX}{rel_name}{SEPARATOR}"
        columns.extend(
            f"{prefix}{idx}" for idx in range(len(related_model.__columns__))
        )
    return columns


def compile_projection_plan(
    query: QuerySet[Any], schema: type[Schema]
) -> core._RowPlan | None:
    """Build a Rust ``_RowPlan`` for ``(query, schema)``, or ``None`` to fall
    back to normal Python model hydration + ``schema.from_model``."""
    model = query.model
    if (
        not query.uses_stock_builders()
        or query.loading.prefetched
        or not _is_plain_model(model)
        or not _is_plain_schema(schema)
    ):
        return None
    columns = set(model.__columns__)
    model_types = model.__field_types__
    fields: list[tuple[str, str, str, bool]] = []
    nested: list[tuple[str, str, str, list[tuple[str, str, str, bool]]]] = []
    for name, spec in schema.specs().items():
        if spec.ftype.schema is not None:
            entry = _nested_plan(query, name, spec)
            if entry is None:
                return None
            nested.append(entry)
            fields.append((name, spec.wire_name, "nested", spec.ftype.nullable))
        elif _scalar_field_ok(name, spec.ftype, columns, model_types):
            fields.append((name, spec.wire_name, spec.ftype.kind, spec.ftype.nullable))
        else:
            return None
    return core._RowPlan(_sql_columns(query), fields, nested)


def _materialize_rows[S: Schema](
    query: QuerySet[Any], schema: type[S], handle: core._ProjectedRows
) -> list[S]:
    return [schema.from_model(query.build(row)) for row in handle.rows()]


async def project_list[S: Schema](
    query: QuerySet[Any], schema: type[S], database: Any
) -> list[S]:
    """Execute ``query`` and project rows directly to ``schema`` in Rust when
    eligible, falling back cleanly to ``Model -> schema.from_model``."""
    plan = compile_projection_plan(query, schema)
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
    plan = compile_projection_plan(window, schema)
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
