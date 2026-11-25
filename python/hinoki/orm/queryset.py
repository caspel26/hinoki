"""Lazy, immutable queries over the Rust compiler: filters, joins, loading,
ordering, counting, pagination and bulk update/delete. Nothing runs until an
``a...`` call (or its sync twin inside a sync handler)."""

from __future__ import annotations

import builtins
import contextlib
import dataclasses
import weakref
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal, overload

from hinoki import core, events
from hinoki.blocking import run_sync
from hinoki.db import Database, current_owner
from hinoki.errors import FieldError, ModelError, MultipleObjectsReturned
from hinoki.orm.context import current_database
from hinoki.orm.keyset import fetch_page
from hinoki.orm.relations import (
    ALIAS_PREFIX,
    STORE,
    Relation,
    forward_value,
    store,
)
from hinoki.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorPage,
    CursorParams,
    Page,
    PageParams,
)
from hinoki.utils import _read_utc

if TYPE_CHECKING:
    from hinoki.core import Row
    from hinoki.events import EventBus
    from hinoki.orm.model import Model
    from hinoki.schema import Schema

OPERATORS = {"gt", "gte", "lt", "lte", "ne", "in", "isnull", "contains"}
SEPARATOR = "__"
IN_CHUNK = 10000
# Dialects without INSERT/UPDATE ... RETURNING: the written rows are re-selected.
NO_RETURNING = frozenset({"mysql", "mariadb"})


@dataclasses.dataclass(frozen=True, slots=True)
class Loading:
    """Relations a query joins for filtering, joins and loads, or prefetches."""

    joined: tuple[str, ...] = ()
    selected: tuple[str, ...] = ()
    prefetched: tuple[str, ...] = ()
    scoped: tuple[tuple[str, QuerySet[Any]], ...] = ()
    """Many-to-many prefetches narrowed by a query of the target model."""


@dataclasses.dataclass(frozen=True, slots=True)
class SubQuery:
    """``column [NOT] IN (SELECT select FROM table ...)``: a condition across a
    to-many relation, run as a subquery so the outer rows never multiply."""

    column: str
    negated: bool
    table: str
    select: str
    model: type[Model]
    joins: tuple[tuple[str, str, str, str, bool, list[str], str | None], ...] = ()
    filters: tuple[tuple[str, str, list[Any]], ...] = ()
    checks: tuple[tuple[str, str, list[Any]], ...] = ()
    """The filters on ``model``'s own columns, for the SQLite Decimal check."""
    subfilters: tuple[SubQuery, ...] = ()

    def raw(self) -> core._RawSub:
        inner = (
            self.table,
            self.select,
            list(self.joins),
            list(self.filters),
            [sub.raw() for sub in self.subfilters],
        )
        return (self.column, self.negated, inner)


_UNSET: Any = object()
_JOINED_PLAN_CACHE: weakref.WeakKeyDictionary[
    type[Model],
    dict[
        tuple[str, ...],
        tuple[core._ModelRows, tuple[core._ModelRows, ...], core._ModelRows],
    ],
] = weakref.WeakKeyDictionary()

_SELECT_SQL_CACHE_MAX = 128
_MAX_CACHED_IN_ARITY = 8
_MAX_I64 = (1 << 63) - 1
_SCALAR_CACHEABLE_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})

type _FilterSig = tuple[tuple[str, str, int], ...]
type _SelectSqlKey = tuple[
    str,
    str,
    _FilterSig,
    tuple[tuple[str, bool], ...],
    bool,
    bool,
    tuple[str, ...],
    tuple[str, ...],
]
_SELECT_SQL_CACHE: weakref.WeakKeyDictionary[
    type[Model], OrderedDict[_SelectSqlKey, str]
] = weakref.WeakKeyDictionary()

_PROJECTION_HOOKS: list[Any] = []


def _projection_hooks() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    object,
]:
    if not _PROJECTION_HOOKS:
        # local import avoids circular dependency with hinoki.orm.model
        from hinoki.orm.projection import (  # noqa: PLC0415
            _MODEL_GET_FALLBACK,
            paginate_projected,
            project_list,
            project_model_one,
            project_one,
        )

        _PROJECTION_HOOKS.extend(
            (
                project_list,
                project_one,
                paginate_projected,
                project_model_one,
                _MODEL_GET_FALLBACK,
            )
        )
    return (
        _PROJECTION_HOOKS[0],
        _PROJECTION_HOOKS[1],
        _PROJECTION_HOOKS[2],
        _PROJECTION_HOOKS[3],
        _PROJECTION_HOOKS[4],
    )


def _valid_bound_int(value: int | None) -> bool:
    return value is None or (type(value) is int and 0 <= value <= _MAX_I64)


def _filter_signature(
    filters: tuple[tuple[str, str, list[Any]], ...],
) -> _FilterSig | None:
    sig: list[tuple[str, str, int]] = []
    for col, op, values in filters:
        arity = len(values)
        if op in _SCALAR_CACHEABLE_OPS and arity == 1:
            sig.append((col, op, 1))
        elif op in ("isnull", "notnull") and arity == 0:
            sig.append((col, op, 0))
        elif op == "in" and arity <= _MAX_CACHED_IN_ARITY:
            sig.append((col, op, arity))
        else:
            return None
    return tuple(sig)


def _select_cache_get(model: type[Model], key: _SelectSqlKey) -> str | None:
    bucket = _SELECT_SQL_CACHE.get(model)
    if bucket is None:
        return None
    sql = bucket.get(key)
    if sql is not None:
        bucket.move_to_end(key)
    return sql


def _select_cache_put(model: type[Model], key: _SelectSqlKey, sql: str) -> None:
    bucket = _SELECT_SQL_CACHE.get(model)
    if bucket is None:
        bucket = OrderedDict()
        _SELECT_SQL_CACHE[model] = bucket
    bucket[key] = sql
    bucket.move_to_end(key)
    if len(bucket) > _SELECT_SQL_CACHE_MAX:
        bucket.popitem(last=False)


def _collect_select_params(
    filters: tuple[tuple[str, str, list[Any]], ...],
    limit_to: int | None,
    offset_by: int | None,
) -> list[Any]:
    params: list[Any] = []
    for _, _, values in filters:
        params.extend(values)
    if limit_to is not None:
        params.append(limit_to)
    if offset_by is not None:
        params.append(offset_by)
    return params


def compile_simple_get_sql(
    model: type[Model],
    filter_columns: tuple[str, ...],
    dialect: str,
) -> str:
    """Compile or return a cached ``SELECT ... WHERE ... LIMIT ?`` template for
    simple equality lookups on ``model``."""
    soft_col = model.__soft_delete__
    sig_items: list[tuple[str, str, int]] = (
        [(soft_col, "isnull", 0)] if soft_col is not None else []
    )
    sig_items.extend((col, "eq", 1) for col in filter_columns)
    sig = tuple(sig_items)
    order = tuple((n.removeprefix("-"), n.startswith("-")) for n in model.__ordering__)
    key: _SelectSqlKey = ("select", dialect, sig, order, True, False, (), ())
    cached = _select_cache_get(model, key)
    if cached is not None:
        return cached
    filters: list[tuple[str, str, list[Any]]] = [
        (col, op, [None] if arity else []) for col, op, arity in sig
    ]
    refuse_sqlite_decimal(model, filters, order, dialect)
    sql, _ = core.compile_query(
        "select",
        model.__table__,
        columns=list(model.__columns__),
        filters=filters,
        order=list(order),
        limit=2,
        offset=None,
        joins=[],
        keyset=None,
        dialect=dialect,
    )
    _select_cache_put(model, key, sql)
    return sql


class QuerySet[M: Model]:
    """Lazy, immutable query description; nothing runs until an ``a...`` call."""

    __slots__ = (
        "filters",
        "implicit_filter_count",
        "limit_to",
        "loading",
        "model",
        "offset_by",
        "order",
    )

    subfilters: tuple[SubQuery, ...] = ()

    def __init__(
        self,
        model: type[M],
        filters: tuple[tuple[str, str, list[Any]], ...] = (),
        order: tuple[tuple[str, bool], ...] | None = None,
        limit_to: int | None = None,
        offset_by: int | None = None,
        loading: Loading = Loading(),
    ) -> None:
        self.model = model
        self.filters = filters
        # ``None``: the model's default ordering (``@ordered_by``); ``()`` is explicit.
        self.order = (
            tuple((n.removeprefix("-"), n.startswith("-")) for n in model.__ordering__)
            if order is None
            else order
        )
        self.limit_to = limit_to
        self.offset_by = offset_by
        self.loading = loading
        self.implicit_filter_count = 0

    def derive(
        self,
        *,
        filters: Any = _UNSET,
        order: Any = _UNSET,
        limit_to: Any = _UNSET,
        offset_by: Any = _UNSET,
        loading: Any = _UNSET,
        implicit_filter_count: Any = _UNSET,
    ) -> QuerySet[M]:
        query = QuerySet(
            self.model,
            self.filters if filters is _UNSET else filters,
            self.order if order is _UNSET else order,
            self.limit_to if limit_to is _UNSET else limit_to,
            self.offset_by if offset_by is _UNSET else offset_by,
            self.loading if loading is _UNSET else loading,
        )
        query.implicit_filter_count = (
            self.implicit_filter_count
            if implicit_filter_count is _UNSET
            else implicit_filter_count
        )
        return query

    def has_explicit_write_filter(self) -> bool:
        """Ignore only the default live/deleted marker added by model scopes."""
        return len(self.filters) > self.implicit_filter_count or bool(self.subfilters)

    def with_subfilters(self, extra: tuple[SubQuery, ...]) -> QuerySet[M]:
        """The query with more conditions across a many-to-many relation."""
        query = LinkedQuerySet(
            self.model,
            self.filters,
            self.order,
            self.limit_to,
            self.offset_by,
            self.loading,
            (*self.subfilters, *extra),
        )
        query.implicit_filter_count = self.implicit_filter_count
        return query

    def filter(self, **conditions: Any) -> QuerySet[M]:
        query = self
        try:
            for key, value in conditions.items():
                query = query.with_condition(key, value)
        except FieldError:
            # A many-to-many key is not a column: retry with the relation
            # filters grouped, so a query on a plain model pays nothing for them.
            if not self.model.__links__:
                raise
            return self.filter_with_links(conditions)
        return query

    def filter_with_links(self, conditions: dict[str, Any]) -> QuerySet[M]:
        query = self
        links: dict[str, list[tuple[str, Any]]] = {}
        for key, value in conditions.items():
            name, _, rest = key.partition(SEPARATOR)
            if name in self.model.__links__:
                links.setdefault(name, []).append((rest, value))
            else:
                query = query.with_condition(key, value)
        for name, items in links.items():
            query = query.with_subfilters((self.model.__links__[name].subquery(items),))
        return query

    def with_condition(self, key: str, value: Any) -> QuerySet[M]:
        relation_name, _, rest = key.partition(SEPARATOR)
        relation = self.model.__relations__.get(relation_name)
        if relation is None or not rest:
            return self.derive(
                filters=(*self.filters, condition(self.model, key, value))
            )
        if not relation.forward:
            raise FieldError(
                f"cannot filter {self.model.__name__} across the reverse relation "
                f"{relation_name!r}; query {relation.model.__name__} instead"
            )
        name, op, values = condition(relation.model, rest, value)
        added = (f"{ALIAS_PREFIX}{relation_name}.{name}", op, values)
        joined = tuple(dict.fromkeys((*self.loading.joined, relation_name)))
        return self.derive(
            filters=(*self.filters, added),
            loading=dataclasses.replace(self.loading, joined=joined),
        )

    def select_related(self, *names: str) -> QuerySet[M]:
        """Load forward relations in the same query (one JOIN each)."""
        for name in names:
            self.forward(name)
        merged = tuple(dict.fromkeys((*self.loading.selected, *names)))
        return self.derive(loading=dataclasses.replace(self.loading, selected=merged))

    def prefetch_related(self, *names: str, **scoped: QuerySet[Any]) -> QuerySet[M]:
        """Load relations with bounded queries per owner chunk.

        ``prefetch_related(tags=Tag.filter(...))`` loads only matching targets
        for a many-to-many relation.
        """
        for name in names:
            if name not in self.model.__links__:
                self.relation(name)
        for name, query in scoped.items():
            if name not in self.model.__links__:
                raise FieldError(
                    f"{self.model.__name__} has no many-to-many relation {name!r}"
                )
            if not isinstance(query, QuerySet):
                raise FieldError(f"prefetch_related({name}=...) takes a query")
            self.model.__links__[name].check_within(query)
        merged = tuple(dict.fromkeys((*self.loading.prefetched, *names, *scoped)))
        kept = tuple(item for item in self.loading.scoped if item[0] not in scoped)
        extra = (*kept, *scoped.items())
        return self.derive(
            loading=dataclasses.replace(self.loading, prefetched=merged, scoped=extra)
        )

    def relation(self, name: str) -> Relation:
        relation = self.model.__relations__.get(name)
        if relation is None:
            known = sorted(self.model.__relations__) or "none"
            raise FieldError(
                f"{self.model.__name__} has no relation {name!r} (relations: {known})"
            )
        return relation

    def forward(self, name: str) -> Relation:
        relation = self.relation(name)
        if not relation.forward:
            raise FieldError(
                f"{self.model.__name__}.{name} is a reverse relation; "
                f"use prefetch_related({name!r})"
            )
        return relation

    def order_by(self, *names: str) -> QuerySet[M]:
        parsed = []
        for name in names:
            column = name.removeprefix("-")
            self.model.check_field(column)
            parsed.append((column, name.startswith("-")))
        return self.derive(order=tuple(parsed))

    def limit(self, count: int) -> QuerySet[M]:
        return self.derive(limit_to=count)

    def offset(self, count: int) -> QuerySet[M]:
        return self.derive(offset_by=count)

    def joins(
        self, counting: bool = False
    ) -> list[tuple[str, str, str, str, bool, list[str], str | None]]:
        """Joins for selected relations (with columns) and filtered ones (without).
        A count needs only the filtered ones: loading a to-one relation cannot
        change the number of rows. A soft-deleted related row does not match the
        join, so it reads as absent."""
        wanted = (
            self.loading.joined
            if counting
            else (
                *self.loading.selected,
                *self.loading.joined,
            )
        )
        names = dict.fromkeys(wanted)
        result = []
        for name in names:
            relation = self.forward(name)
            columns = (
                list(relation.model.__columns__)
                if name in self.loading.selected
                else []
            )
            result.append(
                (
                    relation.model.__table__,
                    f"{ALIAS_PREFIX}{name}",
                    relation.column,
                    relation.model.__primary_key__,
                    True,
                    columns,
                    relation.model.__soft_delete__,
                )
            )
        return result

    def _compile_cached(
        self,
        kind: str,
        dialect: str,
        limit_to: int | None,
        offset_by: int | None,
    ) -> tuple[str, list[Any]] | None:
        if (
            kind not in ("select", "count")
            or not _valid_bound_int(limit_to)
            or not _valid_bound_int(offset_by)
        ):
            return None
        sig = _filter_signature(self.filters)
        if sig is None:
            return None
        selected = () if kind == "count" else self.loading.selected
        key: _SelectSqlKey = (
            kind,
            dialect,
            sig,
            self.order,
            limit_to is not None,
            offset_by is not None,
            selected,
            self.loading.joined,
        )
        cached = _select_cache_get(self.model, key)
        if cached is not None:
            return cached, _collect_select_params(self.filters, limit_to, offset_by)
        refuse_sqlite_decimal(self.model, self.filters, self.order, dialect)
        sql, params = core.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=limit_to,
            offset=offset_by,
            joins=self.joins(counting=kind == "count"),
            keyset=None,
            dialect=dialect,
        )
        _select_cache_put(self.model, key, sql)
        return sql, params

    def compile(
        self,
        kind: str = "select",
        dialect: str = "postgres",
        keyset: tuple[list[tuple[str, bool]], list[Any]] | None = None,
    ) -> tuple[str, list[Any]]:
        if keyset is None:
            cached = self._compile_cached(kind, dialect, self.limit_to, self.offset_by)
            if cached is not None:
                return cached
        refuse_sqlite_decimal(self.model, self.filters, self.order, dialect)
        return core.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=self.limit_to,
            offset=self.offset_by,
            joins=self.joins(counting=kind == "count"),
            keyset=keyset,
            dialect=dialect,
        )

    def compile_through(
        self,
        join: core._RawJoin,
        bridge_filter: core._RawFilter,
        limit: int,
        dialect: str,
    ) -> tuple[str, list[Any]]:
        order = list(self.order)
        key = self.model.__primary_key__
        if not any(column == key for column, _ in order):
            order.append((key, False))
        refuse_sqlite_decimal(self.model, self.filters, tuple(order), dialect)
        for sub in self.subfilters:
            refuse_sqlite_decimal(sub.model, sub.checks, (), dialect)
        return core.compile_query(
            "select",
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=[*self.filters, bridge_filter],
            order=order,
            limit=limit,
            joins=[*self.joins(), join],
            subfilters=[sub.raw() for sub in self.subfilters],
            dialect=dialect,
            linked=True,
        )

    def build(self, row: dict[str, Any]) -> M:
        own = {column: row[column] for column in self.model.__columns__}
        instance = self.model.from_row(own)
        for name in self.loading.selected:
            relation = self.forward(name)
            related_model = relation.model
            prefix = f"{ALIAS_PREFIX}{name}{SEPARATOR}"
            values = {
                c: row[f"{prefix}{index}"]
                for index, c in enumerate(related_model.__columns__)
            }
            key = related_model.__primary_key__
            found = None if values[key] is None else related_model.from_row(values)
            foreign = getattr(instance, relation.column)
            store(instance, name, forward_value(related_model, foreign, found))
        return instance

    async def load(self, instances: list[M]) -> None:
        for name in self.loading.prefetched:
            await self.prefetch(name, instances)

    async def prefetch(self, name: str, instances: list[M]) -> None:
        if name in self.model.__links__:
            within = dict(self.loading.scoped).get(name)
            await self.model.__links__[name].prefetch(instances, within)
            return
        relation = self.relation(name)
        if relation.forward:
            await self.prefetch_forward(relation, instances)
        else:
            await self.prefetch_reverse(relation, instances)

    async def prefetch_forward(self, relation: Relation, instances: list[M]) -> None:
        key = relation.model.__primary_key__
        wanted = {getattr(i, relation.column) for i in instances} - {None}
        rows = await fetch_in(relation.model, key, list(wanted), live_only=True)
        by_key = {getattr(row, key): row for row in rows}
        for instance in instances:
            foreign = getattr(instance, relation.column)
            found = by_key.get(foreign)
            store(
                instance, relation.name, forward_value(relation.model, foreign, found)
            )

    async def prefetch_reverse(self, relation: Relation, instances: list[M]) -> None:
        key = self.model.__primary_key__
        found = await fetch_in(
            relation.model,
            relation.column,
            [getattr(i, key) for i in instances],
            live_only=True,
        )
        children: dict[Any, list[Model]] = {}
        for row in found:
            children.setdefault(getattr(row, relation.column), []).append(row)
        for instance in instances:
            related = children.get(getattr(instance, key), [])
            if relation.one_to_one:
                if len(related) > 1:
                    raise MultipleObjectsReturned(
                        f"{type(instance).__name__}.{relation.name} expected at most "
                        f"one {relation.model.__name__}; found {len(related)}"
                    )
                store(instance, relation.name, related[0] if related else None)
            else:
                store(instance, relation.name, related)

    def __await__(self) -> Generator[Any, None, builtins.list[M]]:
        return self.fetch().__await__()

    def uses_stock_builders(self) -> bool:
        """False when ``build`` or ``_hydrate`` was overridden or patched: the
        native row builder would silently skip that code."""
        return all(
            getattr(getattr(self, name), "__func__", None) is getattr(QuerySet, name)
            for name in ("build", "_hydrate")
        )

    def joined_plan(self, plan: core._ModelRows) -> core._ModelRows | None:
        """The native builder for ``plan``'s model with its ``select_related``
        relations, laid out as ``build`` reads them; ``None`` when a related
        model must go through ``from_row``."""
        selected = self.loading.selected
        per_model = _JOINED_PLAN_CACHE.get(self.model)
        related_plans: list[core._ModelRows] = []
        joins = []
        for name in selected:
            related = self.forward(name).model
            related_plan = related.row_plan()
            # a soft-deleted row is stored as ``Hidden``, which only ``build`` does
            if related_plan is None or related.__soft_delete__ is not None:
                return None
            related_plans.append(related_plan)
            key = list(related.__columns__).index(related.__primary_key__)
            joins.append((name, related_plan, f"{ALIAS_PREFIX}{name}{SEPARATOR}", key))
        related_tuple = tuple(related_plans)
        if per_model is not None:
            cached = per_model.get(selected)
            if cached is not None and cached[0] is plan and cached[1] == related_tuple:
                return cached[2]
        built = plan.joined(joins, STORE)
        if per_model is None:
            per_model = {}
            _JOINED_PLAN_CACHE[self.model] = per_model
        per_model[selected] = (plan, related_tuple, built)
        return built

    def _model_plan(self) -> core._ModelRows | None:
        plan = self.model.row_plan() if self.uses_stock_builders() else None
        if plan is not None and self.loading.selected:
            return self.joined_plan(plan)
        return plan

    async def fetch(self) -> list[M]:
        """Run the query and build the rows; what ``await queryset`` returns."""
        database = current_database()
        sql, params = self.compile(dialect=database.dialect)
        return await self.fetch_compiled(database, sql, params)

    async def fetch_compiled(
        self, database: Database, sql: str, params: list[Any]
    ) -> list[M]:
        """Run ``sql`` compiled from this query and build its rows."""
        plan = self._model_plan()
        fetch_models = getattr(database, "fetch_models", None)
        if plan is not None and fetch_models is not None:
            instances: list[M] = await fetch_models(sql, list(params), plan)
            await self.load(instances)
            return instances
        rows = await database.fetch(sql, *params)
        return await self._hydrate(rows)

    async def _hydrate(self, rows: list[Row]) -> list[M]:
        """Build and load rows already fetched by this query or a safe fallback."""
        instances = [self.build(row) for row in rows]
        await self.load(instances)
        return instances

    async def afirst(self) -> M | None:
        rows = await self.limit(1)
        return rows[0] if rows else None

    async def aget(self) -> M:
        rows = await self.limit(2)
        if not rows:
            raise self.model.NotFound(f"{self.model.__name__} not found")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"{self.model.__name__}: several rows match")
        return rows[0]

    @overload
    async def aupdate(
        self, values: dict[str, Any], *, returning: Literal[True] = ...
    ) -> builtins.list[M]: ...
    @overload
    async def aupdate(
        self, values: dict[str, Any], *, returning: Literal[False]
    ) -> int: ...
    @overload
    async def aupdate(
        self, values: dict[str, Any], *, returning: bool
    ) -> builtins.list[M] | int: ...
    async def aupdate(
        self, values: dict[str, Any], *, returning: bool = True
    ) -> builtins.list[M] | int:
        """Update the rows this query matches (its filters are part of the
        statement) and return them. Ordering and loading are ignored; a query
        with ``limit`` or ``offset`` is refused, because the statement would
        change every matching row. ``returning=False`` reads nothing back and
        returns the row count the database reports instead."""
        if not self.has_explicit_write_filter() or not values:
            raise FieldError("aupdate needs filters and values")
        self.refuse_window("aupdate")
        for name in values:
            self.model.check_field(name)
        timestamp = self.model.__updated_at__
        if timestamp is not None:
            values = {
                **values,
                timestamp: _read_utc(self.model.__clock__),
            }
        values = tracked_values(self.model, values)
        values = self.model.validate_values(values)
        if self.loading.joined or self.subfilters:
            raise FieldError("updates cannot filter across a relation")
        database = current_database()
        sql, params = write_statement(
            self.model,
            "update",
            list(values.items()),
            list(self.filters),
            returning=returning,
            dialect=database.dialect,
        )
        if not returning:
            return await database.execute(sql, *params)
        rows = await written_rows(
            self.model,
            sql,
            params,
            list(self.filters),
            by_key=self.model.__primary_key__ not in values,
        )
        return [self.model.from_row(row) for row in rows]

    async def adelete(self) -> int:
        """Delete the rows this query matches; returns how many. A query
        without filters is refused so it cannot wipe the table, and so is one
        with ``limit`` or ``offset``: the statement would delete every match."""
        if not self.has_explicit_write_filter():
            raise FieldError("adelete needs at least one condition")
        self.refuse_window("adelete")
        if self.loading.joined or self.subfilters:
            raise FieldError(
                "adelete cannot filter across a relation; look the keys up first"
            )
        database = current_database()
        refuse_sqlite_decimal(self.model, self.filters, (), database.dialect)
        soft_column = self.model.__soft_delete__
        if soft_column is not None:
            assignments = [
                (
                    soft_column,
                    _read_utc(self.model.__soft_delete_clock__ or self.model.__clock__),
                )
            ]
            if self.model.__updated_at__ is not None:
                assignments.append(
                    (self.model.__updated_at__, _read_utc(self.model.__clock__))
                )
            filters = [*self.filters, (soft_column, "isnull", [])]
            sql, params = write_statement(
                self.model,
                "update",
                assignments,
                filters,
                returning=False,
                dialect=database.dialect,
            )
        else:
            filters = list(self.filters)
            sql, params = core.compile_query(
                "delete",
                self.model.__table__,
                filters=filters,
                dialect=database.dialect,
            )
        bus = events.listener(events.Deleted, self.model)
        if bus is None:
            return await database.execute(sql, *params)
        return await self.delete_observed(bus, database, sql, params)

    async def arestore(self) -> int:
        """Restore deleted rows selected by this query."""
        column = self.model.__soft_delete__
        if column is None:
            raise ModelError(f"{self.model.__name__} does not support soft deletion")
        if not self.has_explicit_write_filter():
            raise FieldError("arestore needs at least one explicit condition")
        self.refuse_window("arestore")
        if self.loading.joined or self.subfilters:
            raise FieldError("arestore cannot filter across a relation")
        filters = [*self.filters, (column, "notnull", [])]
        return await self.update_deleted_marker(column, None, filters)

    async def ahard_delete(self) -> int:
        """Physically delete rows selected by this query."""
        if not self.has_explicit_write_filter():
            raise FieldError("ahard_delete needs at least one explicit condition")
        self.refuse_window("ahard_delete")
        if self.loading.joined or self.subfilters:
            raise FieldError("ahard_delete cannot filter across a relation")
        database = current_database()
        refuse_sqlite_decimal(self.model, self.filters, (), database.dialect)
        sql, params = core.compile_query(
            "delete",
            self.model.__table__,
            filters=list(self.filters),
            dialect=database.dialect,
        )
        bus = events.listener(events.Deleted, self.model)
        if bus is None:
            return await database.execute(sql, *params)
        return await self.delete_observed(bus, database, sql, params)

    async def update_deleted_marker(
        self, column: str, value: Any, filters: list[Filter]
    ) -> int:
        """Update deletion state and return affected rows."""
        database = current_database()
        refuse_sqlite_decimal(self.model, filters, (), database.dialect)
        assignments = [(column, value)]
        if self.model.__updated_at__ is not None:
            assignments.append(
                (self.model.__updated_at__, _read_utc(self.model.__clock__))
            )
        sql, params = write_statement(
            self.model,
            "update",
            assignments,
            filters,
            returning=False,
            dialect=database.dialect,
        )
        return await database.execute(sql, *params)

    async def delete_observed(
        self, bus: EventBus, database: Database, sql: str, params: list[Any]
    ) -> int:
        """``adelete`` that emits ``Deleted`` (when it removed rows), inside a
        transaction when an ``in_transaction`` handler may fail it."""
        guard = (
            write_transaction(database)
            if bus.wants_transaction(events.Deleted, self.model)
            else contextlib.nullcontext()
        )
        async with guard:
            count = await database.execute(sql, *params)
            if count:
                await bus.emit(events.Deleted(self.model, count))
        return count

    def refuse_window(self, action: str) -> None:
        if self.limit_to is not None or self.offset_by is not None:
            key = self.model.__primary_key__
            raise FieldError(
                f"{action} does not apply limit/offset and would change every "
                f"matching row; fetch the rows first, then filter({key}__in=[...])"
            )

    async def acount(self) -> int:
        database = current_database()
        sql, params = self.derive(order=(), limit_to=None, offset_by=None).compile(
            "count", database.dialect
        )
        rows = await database.fetch(sql, *params)
        return int(rows[0]["count"])

    async def aexists(self) -> bool:
        return await self.afirst() is not None

    def list(self) -> builtins.list[M]:
        """Sync ``await queryset`` for sync handlers."""
        return run_sync(self.fetch())

    def first(self) -> M | None:
        return run_sync(self.afirst())

    def get(self) -> M:
        return run_sync(self.aget())

    @overload
    def update(
        self, values: dict[str, Any], *, returning: Literal[True] = ...
    ) -> builtins.list[M]: ...
    @overload
    def update(self, values: dict[str, Any], *, returning: Literal[False]) -> int: ...
    @overload
    def update(
        self, values: dict[str, Any], *, returning: bool
    ) -> builtins.list[M] | int: ...
    def update(
        self, values: dict[str, Any], *, returning: bool = True
    ) -> builtins.list[M] | int:
        """Sync ``aupdate``."""
        if not returning:
            return run_sync(self.aupdate(values, returning=False))
        return run_sync(self.aupdate(values))

    def delete(self) -> int:
        """Sync ``adelete``."""
        return run_sync(self.adelete())

    def restore(self) -> int:
        """Sync ``arestore``."""
        return run_sync(self.arestore())

    def hard_delete(self) -> int:
        """Sync ``ahard_delete``."""
        return run_sync(self.ahard_delete())

    def count(self) -> int:
        return run_sync(self.acount())

    def exists(self) -> bool:
        return run_sync(self.aexists())

    def paginate(
        self, params: PageParams | None = None, *, concurrent: bool = False
    ) -> Page[M]:
        return run_sync(self.apaginate(params, concurrent=concurrent))

    def project[S: Schema](self, schema: type[S]) -> builtins.list[S]:
        """Sync ``aproject``."""
        return run_sync(self.aproject(schema))

    def get_as[S: Schema](self, schema: type[S]) -> S:
        """Sync ``aget_as``."""
        return run_sync(self.aget_as(schema))

    def get_or_none_as[S: Schema](self, schema: type[S]) -> S | None:
        """Sync ``aget_or_none_as``."""
        return run_sync(self.aget_or_none_as(schema))

    def paginate_as[S: Schema](
        self,
        schema: type[S],
        params: PageParams | None = None,
        *,
        concurrent: bool = False,
    ) -> Page[S]:
        """Sync ``apaginate_as``."""
        return run_sync(self.apaginate_as(schema, params, concurrent=concurrent))

    def stably_ordered(self) -> QuerySet[M]:
        """The queryset with the primary key as the last sort key, so rows with
        equal sort values keep a fixed position from one page to the next."""
        key = self.model.__primary_key__
        if any(column == key for column, _ in self.order):
            return self
        return self.derive(order=(*self.order, (key, False)))

    async def aproject[S: Schema](self, schema: type[S]) -> builtins.list[S]:
        """Project matching rows directly to ``schema`` in Rust when eligible,
        returning a lazy ``list[S]`` that emits pre-encoded JSON with zero
        Python ``Model``/``Schema`` allocations and materializes transparently
        on Python access without a second database query."""
        project_list, _, _, _, _ = _projection_hooks()
        result: builtins.list[S] = await project_list(self, schema, current_database())
        return result

    async def aget_as[S: Schema](self, schema: type[S]) -> S:
        """Fetch exactly one row and project it directly to ``schema`` in Rust
        when eligible; raises ``Model.NotFound`` or ``MultipleObjectsReturned``
        otherwise."""
        _, project_one, _, _, _ = _projection_hooks()
        found: S | None = await project_one(
            self, schema, current_database(), allow_none=False
        )
        if found is None:
            msg = f"{self.model.__name__} matching query does not exist."
            raise self.model.NotFound(msg)
        return found

    async def aget_or_none_as[S: Schema](self, schema: type[S]) -> S | None:
        """Fetch at most one row and project it directly to ``schema`` in Rust
        when eligible; returns ``None`` if no row matches and raises
        ``MultipleObjectsReturned`` if several match."""
        _, project_one, _, _, _ = _projection_hooks()
        found: S | None = await project_one(
            self, schema, current_database(), allow_none=True
        )
        return found

    async def apaginate_as[S: Schema](
        self,
        schema: type[S],
        params: PageParams | None = None,
        *,
        concurrent: bool = False,
    ) -> Page[S]:
        """Paginate and project matching rows directly to ``schema`` in Rust
        when eligible, returning a lazy ``Page[S]`` that emits pre-encoded JSON
        and materializes ``.items`` transparently without a second query."""
        _, _, paginate_projected, _, _ = _projection_hooks()
        resolved = PageParams(1, DEFAULT_PAGE_SIZE) if params is None else params
        page: Page[S] = await paginate_projected(
            self, schema, resolved, current_database(), concurrent=concurrent
        )
        return page

    async def apaginate(
        self, params: PageParams | None = None, *, concurrent: bool = False
    ) -> Page[M]:
        """One page plus the total. The primary key is the final sort key so
        pages are stable.

        With ``concurrent=True``, the row and count queries run together on
        pools with capacity for at least two connections (outside an active
        transaction); SQLite, single-connection pools and active transactions
        always run them sequentially.
        """
        resolved = PageParams(1, DEFAULT_PAGE_SIZE) if params is None else params
        ordered = self.stably_ordered()
        if not concurrent:
            total = await self.acount()
            window = ordered.limit(resolved.page_size).offset(resolved.offset)
            return Page(await window, resolved.page, resolved.page_size, total)
        window = ordered.limit(resolved.page_size).offset(resolved.offset)
        items, total = await self._fetch_page_concurrent(window)
        return Page(items, resolved.page, resolved.page_size, total)

    async def _fetch_page_concurrent(
        self, window: QuerySet[M]
    ) -> tuple[builtins.list[M], int]:
        """Fetch rows and total count for ``window`` via the native page path,
        falling back cleanly when a custom builder or database stub is used."""
        database = current_database()
        rows_sql, rows_params = window.compile(dialect=database.dialect)
        count_sql, count_params = self.derive(
            order=(), limit_to=None, offset_by=None
        ).compile("count", database.dialect)
        plan = window._model_plan()
        fetch_page_models = getattr(database, "fetch_page_models", None)
        if plan is not None and fetch_page_models is not None:
            instances: builtins.list[M]
            instances, total = await fetch_page_models(
                rows_sql,
                list(rows_params),
                count_sql,
                list(count_params),
                plan,
                concurrent=True,
            )
            await window.load(instances)
            return instances, total
        fetch_page_rows = getattr(database, "fetch_page_rows", None)
        if fetch_page_rows is not None:
            rows, total = await fetch_page_rows(
                rows_sql,
                list(rows_params),
                count_sql,
                list(count_params),
                concurrent=True,
            )
            return await window._hydrate(rows), total
        total = await self.acount()
        return await window, total

    async def apaginate_cursor(self, params: CursorParams) -> CursorPage[M]:
        """One keyset page: ordered like this query with the primary key as the
        final sort key, bounded by the signed cursor in ``params``, without a
        COUNT. This query's filters apply; its limit and offset do not."""
        return await fetch_page(self, params)

    def paginate_cursor(self, params: CursorParams) -> CursorPage[M]:
        return run_sync(self.apaginate_cursor(params))


# Row-wire selection must compare against the ORM's original methods, not
# whichever implementations happen to be installed when an app is included.
ROW_WIRE_METHODS = (
    "derive",
    "order_by",
    "limit",
    "offset",
    "apaginate",
    "apaginate_cursor",
    "__await__",
    "fetch",
    "fetch_compiled",
    "_hydrate",
    "build",
    "load",
)
ROW_WIRE_METHODS_ORIGINAL = tuple(getattr(QuerySet, name) for name in ROW_WIRE_METHODS)


TEXT_DECIMAL_OPERATORS = frozenset({"eq", "ne", "in", "isnull", "notnull"})


def decimal_value(model: type[Model], name: str, value: Any) -> Decimal:
    """A filter value for a ``Decimal`` column: exact numbers only, never a float."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise FieldError(
            f"{model.__name__}.{name} is a Decimal column: filter with a Decimal, "
            f"an int or a numeric string, not {type(value).__name__}"
        )
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise FieldError(
            f"{model.__name__}.{name}: {value!r} is not a number"
        ) from None
    if not number.is_finite():
        raise FieldError(f"{model.__name__}.{name}: {value!r} is not a finite number")
    return number


def decimal_columns(model: type[Model], column: str) -> bool:
    """Whether ``column`` (own, or ``rel_<name>.<column>`` through a join) is a
    ``Decimal`` column."""
    owner, _, name = column.rpartition(".")
    if owner:
        relation = model.__relations__.get(owner.removeprefix(ALIAS_PREFIX))
        if relation is None:
            return False  # pragma: no cover - callers pass valid relation names
        model = relation.model
    field_type = model.__field_types__.get(name)
    return field_type is not None and field_type.kind == "decimal"


def refuse_sqlite_decimal(
    model: type[Model],
    filters: Iterable[tuple[str, str, list[Any]]],
    order: Iterable[tuple[str, bool]],
    dialect: str,
) -> None:
    """SQLite keeps a ``Decimal`` as text, which compares by spelling
    (``12.3`` and ``12.30`` differ) and orders alphabetically: only equality
    is allowed there, and no ordering."""
    if dialect != "sqlite":
        return
    for column, op, _ in filters:
        if op not in TEXT_DECIMAL_OPERATORS and decimal_columns(model, column):
            raise FieldError(
                f"{column} is a Decimal column stored as text on SQLite: "
                f"{op} comparisons are not supported there; use PostgreSQL or MySQL"
            )
    for column, _ in order:
        if decimal_columns(model, column):
            raise FieldError(
                f"{column} is a Decimal column stored as text on SQLite: "
                "ordering by it is not supported there; use PostgreSQL or MySQL"
            )


class LinkedQuerySet[M: Model](QuerySet[M]):
    """A query with conditions across a many-to-many relation. Plain queries
    never carry them, so they pay nothing for the feature."""

    __slots__ = ("subfilters",)

    def __init__(
        self,
        model: type[M],
        filters: tuple[tuple[str, str, list[Any]], ...] = (),
        order: tuple[tuple[str, bool], ...] | None = None,
        limit_to: int | None = None,
        offset_by: int | None = None,
        loading: Loading = Loading(),
        subfilters: tuple[SubQuery, ...] = (),
    ) -> None:
        super().__init__(model, filters, order, limit_to, offset_by, loading)
        self.subfilters = subfilters

    def derive(
        self,
        *,
        filters: Any = _UNSET,
        order: Any = _UNSET,
        limit_to: Any = _UNSET,
        offset_by: Any = _UNSET,
        loading: Any = _UNSET,
        implicit_filter_count: Any = _UNSET,
        subfilters: Any = _UNSET,
    ) -> QuerySet[M]:
        query = LinkedQuerySet(
            self.model,
            self.filters if filters is _UNSET else filters,
            self.order if order is _UNSET else order,
            self.limit_to if limit_to is _UNSET else limit_to,
            self.offset_by if offset_by is _UNSET else offset_by,
            self.loading if loading is _UNSET else loading,
            self.subfilters if subfilters is _UNSET else subfilters,
        )
        query.implicit_filter_count = (
            self.implicit_filter_count
            if implicit_filter_count is _UNSET
            else implicit_filter_count
        )
        return query

    def compile(
        self,
        kind: str = "select",
        dialect: str = "postgres",
        keyset: tuple[list[tuple[str, bool]], list[Any]] | None = None,
    ) -> tuple[str, list[Any]]:
        refuse_sqlite_decimal(self.model, self.filters, self.order, dialect)
        for sub in self.subfilters:
            refuse_sqlite_decimal(sub.model, sub.checks, (), dialect)
        return core.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=self.limit_to,
            offset=self.offset_by,
            joins=self.joins(counting=kind == "count"),
            keyset=keyset,
            subfilters=[sub.raw() for sub in self.subfilters],
            dialect=dialect,
        )


def condition(model: type[Model], key: str, value: Any) -> tuple[str, str, list[Any]]:
    """Parse ``name``, ``name__op`` into ``(column, operator, values)``."""
    name, _, suffix = key.rpartition(SEPARATOR)
    if not name or suffix not in OPERATORS:
        name, suffix = key, "eq"
    model.check_field(name)
    if suffix == "isnull":
        return name, "isnull" if value else "notnull", []
    if value is None:
        if suffix not in {"eq", "ne"}:
            raise FieldError(f"{key} cannot compare with None")
        return name, "isnull" if suffix == "eq" else "notnull", []
    values = condition_values(key, suffix, value)
    if model.__field_types__[name].kind == "decimal":
        if suffix == "contains":
            raise FieldError(f"{key}: contains does not apply to a Decimal column")
        values = [decimal_value(model, name, item) for item in values]
    return name, suffix, values


def condition_values(key: str, suffix: str, value: Any) -> list[Any]:
    """The values one filter compares with: ``__in`` takes a collection, never a
    string, which would be split into its characters."""
    if suffix != "in":
        return [value]
    if isinstance(value, str | bytes):
        raise FieldError(f"{key} needs a list of values, not a string")
    return list(value)


Filter = tuple[str, str, list[Any]]


def write_statement(
    model: type[Model],
    kind: str,
    assignments: list[tuple[str, Any]],
    filters: list[Filter],
    *,
    returning: bool,
    dialect: str,
) -> tuple[str, list[Any]]:
    """An INSERT or UPDATE that returns every column when ``returning`` is set
    and the dialect supports it, and nothing otherwise."""
    reads = returning and dialect not in NO_RETURNING
    if kind == "update":
        assigned = dict(assignments)
        for track in model.__status_tracks__:
            if track.field in assigned and track.changed_at not in assigned:
                assigned[track.changed_at] = _read_utc(track.clock or model.__clock__)
        assignments = list(assigned.items())
    refuse_sqlite_decimal(model, filters, (), dialect)
    return core.compile_query(
        kind,
        model.__table__,
        assignments=assignments,
        filters=filters,
        returning=list(model.__columns__) if reads else [],
        dialect=dialect,
    )


def tracked_values(model: type[Model], values: dict[str, Any]) -> dict[str, Any]:
    """Add the changed-at values for status fields present in a bulk update."""
    tracked = dict(values)
    for item in model.__status_tracks__:
        if item.field in tracked and item.changed_at not in tracked:
            tracked[item.changed_at] = _read_utc(item.clock or model.__clock__)
    return tracked


def write_transaction(database: Database) -> AbstractAsyncContextManager[Any]:
    """The transaction a write runs in: a savepoint when one is open. When the
    open transaction belongs to another task (a sync handler's ``run_sync``
    writes from a helper task while its owner waits), that transaction already
    rolls everything back on an error, so none is nested."""
    active = database.active()
    if active is not None and current_owner() is not active.owner:
        return contextlib.nullcontext()
    return database.atomic()


async def written_rows(
    model: type[Model],
    sql: str,
    params: list[Any],
    filters: list[Filter],
    *,
    by_key: bool = False,
) -> list[Row]:
    """The stored rows after a write compiled by ``write_statement``. MySQL and
    MariaDB have no RETURNING: the rows are selected on the same transaction
    connection so the return value remains stable. With ``by_key`` the matching
    primary keys are locked before the write and read back afterwards, so rows
    that stop matching ``filters`` because of the write are still returned."""
    database = current_database()
    if database.dialect not in NO_RETURNING:
        return await database.fetch(sql, *params)
    async with write_transaction(database):
        key = model.__primary_key__
        # A key lookup cannot leave its own filter, so it needs no pre-read.
        if by_key and any(name != key or op != "eq" for name, op, _ in filters):
            keys = await locked_keys(model, filters, database.dialect)
            if not keys:
                return []
            filters = [(key, "in", keys)]
        await database.execute(sql, *params)
        rows: list[Row] = []
        for group in read_back_groups(key, filters):
            select_sql, select_params = core.compile_query(
                "select",
                model.__table__,
                columns=list(model.__columns__),
                filters=group,
                dialect=database.dialect,
            )
            rows.extend(await database.fetch(select_sql, *select_params))
        return rows


def read_back_groups(key: str, filters: list[Filter]) -> list[list[Filter]]:
    """``filters``, or the locked keys split so each statement stays small."""
    if len(filters) != 1 or filters[0][:2] != (key, "in"):
        return [filters]
    keys = filters[0][2]
    return [
        [(key, "in", keys[start : start + IN_CHUNK])]
        for start in range(0, len(keys), IN_CHUNK)
    ]


async def locked_keys(
    model: type[Model], filters: list[Filter], dialect: str
) -> list[Any]:
    """The primary keys ``filters`` match, locked in key order so two writers
    cannot take the same rows in opposite orders, until the transaction ends."""
    key = model.__primary_key__
    sql, params = core.compile_query(
        "select",
        model.__table__,
        columns=[key],
        filters=filters,
        order=[(key, False)],
        dialect=dialect,
    )
    rows = await current_database().fetch(sql + " FOR UPDATE", *params)
    return [row[key] for row in rows]


async def fetch_in(
    model: type[Model], column: str, values: list[Any], *, live_only: bool = False
) -> list[Any]:
    """Rows of ``model`` whose ``column`` is one of ``values`` (chunked so the
    statement stays far below PostgreSQL's parameter limit)."""
    found: list[Model] = []
    for start in range(0, len(values), IN_CHUNK):
        chunk = values[start : start + IN_CHUNK]
        base = model.all() if live_only else QuerySet(model)
        found.extend(await base.filter(**{f"{column}__in": chunk}))
    return found


async def delete_keys(model: type[Model], keys: list[Any]) -> int:
    """Delete the rows whose primary key is in ``keys``, chunked like
    ``fetch_in``; returns how many were deleted."""
    key = model.__primary_key__
    deleted = 0
    for start in range(0, len(keys), IN_CHUNK):
        chunk = keys[start : start + IN_CHUNK]
        deleted += await QuerySet(model).filter(**{f"{key}__in": chunk}).adelete()
    return deleted
