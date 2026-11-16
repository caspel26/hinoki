"""Lazy, immutable queries over the Rust compiler: filters, joins, loading,
ordering, counting, pagination and bulk update/delete. Nothing runs until an
``a...`` call (or its sync twin inside a sync handler)."""

from __future__ import annotations

import builtins
import dataclasses
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, Literal, overload

from hinoki import core
from hinoki.blocking import run_sync
from hinoki.errors import FieldError, MultipleObjectsReturned
from hinoki.orm.context import current_database
from hinoki.orm.relations import ALIAS_PREFIX, Relation, store
from hinoki.pagination import Page, PageParams
from hinoki.utils import _read_utc

if TYPE_CHECKING:
    from hinoki.core import Row
    from hinoki.orm.model import Model

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


class QuerySet[M: Model]:
    """Lazy, immutable query description; nothing runs until an ``a...`` call."""

    def __init__(
        self,
        model: type[M],
        filters: tuple[tuple[str, str, list[Any]], ...] = (),
        order: tuple[tuple[str, bool], ...] | None = None,
        limit_to: int | None = None,
        offset_by: int | None = None,
        loading: Loading = Loading(),  # noqa: B008 - frozen, safe to share
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

    def derive(self, **changes: Any) -> QuerySet[M]:
        state = {
            "filters": self.filters,
            "order": self.order,
            "limit_to": self.limit_to,
            "offset_by": self.offset_by,
            "loading": self.loading,
        }
        return QuerySet(self.model, **{**state, **changes})

    def filter(self, **conditions: Any) -> QuerySet[M]:
        query = self
        for key, value in conditions.items():
            query = query.with_condition(key, value)
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

    def prefetch_related(self, *names: str) -> QuerySet[M]:
        """Load relations (forward or reverse) with one extra query each."""
        for name in names:
            self.relation(name)
        merged = tuple(dict.fromkeys((*self.loading.prefetched, *names)))
        return self.derive(loading=dataclasses.replace(self.loading, prefetched=merged))

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
    ) -> list[tuple[str, str, str, str, bool, list[str]]]:
        """Joins for selected relations (with columns) and filtered ones (without).
        A count needs only the filtered ones: loading a to-one relation cannot
        change the number of rows."""
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
                )
            )
        return result

    def compile(
        self, kind: str = "select", dialect: str = "postgres"
    ) -> tuple[str, list[Any]]:
        return core.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=self.limit_to,
            offset=self.offset_by,
            joins=self.joins(counting=kind == "count"),
            dialect=dialect,
        )

    def build(self, row: dict[str, Any]) -> M:
        own = {column: row[column] for column in self.model.__columns__}
        instance = self.model.from_row(own)
        for name in self.loading.selected:
            related_model = self.forward(name).model
            prefix = f"{ALIAS_PREFIX}{name}{SEPARATOR}"
            values = {
                c: row[f"{prefix}{index}"]
                for index, c in enumerate(related_model.__columns__)
            }
            key = related_model.__primary_key__
            store(
                instance,
                name,
                None if values[key] is None else related_model.from_row(values),
            )
        return instance

    async def load(self, instances: list[M]) -> None:
        for name in self.loading.prefetched:
            await self.prefetch(name, instances)

    async def prefetch(self, name: str, instances: list[M]) -> None:
        relation = self.relation(name)
        if relation.forward:
            await self.prefetch_forward(relation, instances)
        else:
            await self.prefetch_reverse(relation, instances)

    async def prefetch_forward(self, relation: Relation, instances: list[M]) -> None:
        key = relation.model.__primary_key__
        wanted = {getattr(i, relation.column) for i in instances} - {None}
        found = await fetch_in(relation.model, key, list(wanted))
        by_key = {getattr(row, key): row for row in found}
        for instance in instances:
            store(
                instance, relation.name, by_key.get(getattr(instance, relation.column))
            )

    async def prefetch_reverse(self, relation: Relation, instances: list[M]) -> None:
        key = self.model.__primary_key__
        found = await fetch_in(
            relation.model, relation.column, [getattr(i, key) for i in instances]
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

    async def fetch(self) -> list[M]:
        """Run the query and build the rows; what ``await queryset`` returns."""
        database = current_database()
        sql, params = self.compile(dialect=database.dialect)
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
        if not self.filters or not values:
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
        self.model.validate_values(values)
        if self.loading.joined:
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
        rows = await written_rows(self.model, sql, params, list(self.filters))
        return [self.model.from_row(row) for row in rows]

    async def adelete(self) -> int:
        """Delete the rows this query matches; returns how many. A query
        without filters is refused so it cannot wipe the table, and so is one
        with ``limit`` or ``offset``: the statement would delete every match."""
        if not self.filters:
            raise FieldError("adelete needs at least one condition")
        self.refuse_window("adelete")
        if self.loading.joined:
            raise FieldError(
                "adelete cannot filter across a relation; look the keys up first"
            )
        database = current_database()
        sql, params = core.compile_query(
            "delete",
            self.model.__table__,
            filters=list(self.filters),
            dialect=database.dialect,
        )
        return await database.execute(sql, *params)

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

    def count(self) -> int:
        return run_sync(self.acount())

    def exists(self) -> bool:
        return run_sync(self.aexists())

    def paginate(self, params: PageParams) -> Page[M]:
        return run_sync(self.apaginate(params))

    async def apaginate(self, params: PageParams) -> Page[M]:
        """One page plus the total. Without ``order_by`` the primary key orders
        the rows so pages are stable."""
        ordered = self if self.order else self.order_by(self.model.__primary_key__)
        total = await self.acount()
        window = ordered.limit(params.page_size).offset(params.offset)
        return Page(await window, params.page, params.page_size, total)


# Row-wire selection must compare against the ORM's original methods, not
# whichever implementations happen to be installed when an app is included.
ROW_WIRE_METHODS = (
    "derive",
    "order_by",
    "limit",
    "offset",
    "apaginate",
    "__await__",
    "fetch",
    "_hydrate",
    "build",
    "load",
)
ROW_WIRE_METHODS_ORIGINAL = tuple(getattr(QuerySet, name) for name in ROW_WIRE_METHODS)


def condition(model: type[Model], key: str, value: Any) -> tuple[str, str, list[Any]]:
    """Parse ``name``, ``name__op`` into ``(column, operator, values)``."""
    name, _, suffix = key.rpartition(SEPARATOR)
    if not name or suffix not in OPERATORS:
        name, suffix = key, "eq"
    model.check_field(name)
    if suffix == "in":
        return name, "in", list(value)
    if suffix == "isnull":
        return name, "isnull" if value else "notnull", []
    if value is None:
        if suffix not in {"eq", "ne"}:
            raise FieldError(f"{key} cannot compare with None")
        return name, "isnull" if suffix == "eq" else "notnull", []
    return name, suffix, [value]


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
    return core.compile_query(
        kind,
        model.__table__,
        assignments=assignments,
        filters=filters,
        returning=list(model.__columns__) if reads else [],
        dialect=dialect,
    )


async def written_rows(
    model: type[Model], sql: str, params: list[Any], filters: list[Filter]
) -> list[Row]:
    """The stored rows after a write compiled by ``write_statement``. MySQL and
    MariaDB have no RETURNING: the rows matching ``filters`` are selected on the
    same transaction connection so the return value remains stable."""
    database = current_database()
    if database.dialect not in NO_RETURNING:
        return await database.fetch(sql, *params)
    async with database.transaction():
        await database.execute(sql, *params)
        select_sql, select_params = core.compile_query(
            "select",
            model.__table__,
            columns=list(model.__columns__),
            filters=filters,
            dialect=database.dialect,
        )
        return await database.fetch(select_sql, *select_params)


async def fetch_in(model: type[Model], column: str, values: list[Any]) -> list[Any]:
    """Rows of ``model`` whose ``column`` is one of ``values`` (chunked so the
    statement stays far below PostgreSQL's parameter limit)."""
    found: list[Model] = []
    for start in range(0, len(values), IN_CHUNK):
        chunk = values[start : start + IN_CHUNK]
        found.extend(await QuerySet(model).filter(**{f"{column}__in": chunk}))
    return found
