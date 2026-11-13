"""Active Record models over the Rust query compiler.

Single-table models with equality/comparison/membership filters, ordering,
counting and pagination, plus create/get/update/delete, and foreign keys that
are loaded only on request (``select_related`` joins, ``prefetch_related``
runs one extra query). SQL is produced by the native compiler from validated
field names; values are always bound parameters.

Typing note: construction (``Post(title=...)``) is checked statically;
keyword-style helpers such as ``Post.acreate(**values)`` are not (see
docs/TYPING.md) until generated stubs exist.
"""

from __future__ import annotations

import builtins
import copy
import dataclasses
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar, Self, cast, dataclass_transform

from hinoki import native
from hinoki.blocking import run_sync
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    MultipleObjectsReturned,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.pagination import Page, PageParams
from hinoki.relations import ALIAS_PREFIX, ON_DELETE, Related, Relation, store

OPERATORS = {"gt", "gte", "lt", "lte", "ne", "in", "isnull"}
SEPARATOR = "__"
IN_CHUNK = 10000
MARK = "hinoki_field"
SNAPSHOT = "hinoki_snapshot"

database_var: ContextVar[Database | None] = ContextVar("hinoki_database", default=None)


@contextmanager
def use_database(database: Database) -> Iterator[Database]:
    """Make ``database`` the default for models inside the block (the ASGI
    adapter does this for every request when a ``Database`` is provided)."""
    token = database_var.set(database)
    try:
        yield database
    finally:
        database_var.reset(token)


def current_database() -> Database:
    database = database_var.get()
    if database is None:
        raise RuntimeError(
            "no database is active: provide a Database (DatabaseModule) or use "
            "hinoki.orm.use_database(...)"
        )
    return database


@dataclasses.dataclass(frozen=True, slots=True)
class FieldMeta:
    primary_key: bool = False
    unique: bool = False
    max_length: int | None = None
    references: type[Model] | None = None
    on_delete: str = ""
    related_name: str = ""


def field(
    *,
    primary_key: bool = False,
    unique: bool = False,
    max_length: int | None = None,
    default: Any = dataclasses.MISSING,
    default_factory: Callable[[], Any] | Any = dataclasses.MISSING,
) -> Any:
    """Declare a column.

    ``id: UUID = field(primary_key=True, default_factory=uuid4)``
    """
    meta = FieldMeta(primary_key, unique, max_length)
    return dataclasses.field(
        default=default, default_factory=default_factory, metadata={MARK: meta}
    )


def foreign_key(
    model: type[Model],
    *,
    on_delete: str,
    related_name: str = "",
    unique: bool = False,
    default: Any = dataclasses.MISSING,
) -> Any:
    """Declare a column that references ``model``'s primary key.

    ``author_id: UUID = foreign_key(User, on_delete="restrict", related_name="posts")``

    ``on_delete`` is required (``restrict``, ``cascade``, ``set_null`` or
    ``no_action``) so a deletion rule is always a decision. The relation is
    named after the column without ``_id`` and is loaded only by
    ``select_related`` / ``prefetch_related``.
    """
    if on_delete not in ON_DELETE:
        raise RegistrationError(f"on_delete must be one of {sorted(ON_DELETE)}")
    meta = FieldMeta(
        unique=unique, references=model, on_delete=on_delete, related_name=related_name
    )
    return dataclasses.field(default=default, metadata={MARK: meta})


@dataclass_transform(kw_only_default=True, field_specifiers=(field,))
class Model:
    """Base class: ``class Post(Model, table="posts"): ...``."""

    __table__: ClassVar[str] = ""
    __columns__: ClassVar[tuple[str, ...]] = ()
    __primary_key__: ClassVar[str] = ""
    __relations__: ClassVar[dict[str, Relation]] = {}
    NotFound: ClassVar[type[ObjectNotFound]] = ObjectNotFound

    def __init_subclass__(cls, table: str = "", **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not table:
            raise RegistrationError(f"{cls.__name__}: table=... is required")
        dataclasses.dataclass(kw_only=True)(cls)
        fields = dataclasses.fields(cast(Any, cls))
        keys = [f.name for f in fields if f.metadata.get(MARK, FieldMeta()).primary_key]
        if len(keys) != 1:
            raise RegistrationError(f"{cls.__name__}: exactly one primary_key field")
        cls.__table__ = table
        cls.__columns__ = tuple(f.name for f in fields)
        cls.__primary_key__ = keys[0]
        cls.check_names(cls.__columns__)
        cls.__relations__ = {
            name: relation
            for base in cls.__mro__[1:]
            for name, relation in getattr(base, "__relations__", {}).items()
            if relation.forward
        }
        cls.register_relations(fields)
        cls.NotFound = type(
            "NotFound", (ObjectNotFound,), {"__qualname__": f"{cls.__name__}.NotFound"}
        )

    @classmethod
    def check_names(cls, columns: tuple[str, ...]) -> None:
        """A column named like a Model method would hide it on every instance."""
        reserved = sorted(n for n in columns if n in dir(Model) or n.startswith("__"))
        if reserved:
            raise RegistrationError(
                f"{cls.__name__}: field names {reserved} collide with Model attributes"
            )

    @classmethod
    def register_relations(cls, fields: tuple[dataclasses.Field[Any], ...]) -> None:
        """Foreign keys declared by this class (inherited ones come with the
        base). Every check runs before anything is attached, so a failing
        definition leaves the referenced models untouched."""
        own = cls.__dict__.get("__annotations__", {})
        pending: list[tuple[type[Model], Relation]] = []
        for column in fields:
            meta: FieldMeta = column.metadata.get(MARK, FieldMeta())
            if meta.references is None or column.name not in own:
                continue
            name = column.name.removesuffix("_id")
            if name == column.name:
                raise RegistrationError(
                    f"{cls.__name__}.{column.name}: foreign key columns end in _id"
                )
            pending.append((cls, Relation(name, meta.references, column.name, True)))
            if meta.related_name:
                back = Relation(meta.related_name, cls, column.name, False)
                pending.append((meta.references, back))
        for owner, relation in pending:
            owner.check_attach(relation.name, relation)
        for owner, relation in pending:
            owner.attach(relation.name, relation)

    @classmethod
    def check_attach(cls, name: str, relation: Relation) -> None:
        existing = cls.__relations__.get(name)
        redefined = (
            existing is not None
            and not existing.forward
            and not relation.forward
            and (existing.model.__module__, existing.model.__qualname__)
            == (relation.model.__module__, relation.model.__qualname__)
        )  # the same model defined again (reload, notebook re-run)
        taken = (
            name in cls.__columns__
            or (name in cls.__relations__ and not redefined)
            or (
                hasattr(cls, name)
                and not isinstance(inspect.getattr_static(cls, name), Related)
            )
        )
        if taken:
            raise RegistrationError(f"{cls.__name__} already has an attribute {name!r}")

    @classmethod
    def attach(cls, name: str, relation: Relation) -> None:
        cls.__relations__[name] = relation
        setattr(cls, name, Related(name, relation))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        instance = cls(**row)
        instance.remember()
        return instance

    def remember(self) -> None:
        """Record the stored values, so ``asave`` writes only what changed."""
        columns = type(self).__columns__
        self.__dict__[SNAPSHOT] = {c: copy.deepcopy(getattr(self, c)) for c in columns}

    async def asave(self) -> Self:
        """Insert a new instance, or update the columns changed since it was
        loaded. Typed: ``Post(title="x", ...)`` is checked by the type checker."""
        cls = type(self)
        stored = self.__dict__.get(SNAPSHOT)
        if stored is None:
            return await self.insert()
        key = cls.__primary_key__
        if getattr(self, key) != stored[key]:
            raise FieldError(f"{cls.__name__}: the primary key cannot be changed")
        changed = {
            c: getattr(self, c)
            for c in cls.__columns__
            if c != key and getattr(self, c) != stored[c]
        }
        if changed:
            updated = await cls.aupdate(**{key: stored[key]}, **changed)
            self.refresh(updated)
        return self

    async def insert(self) -> Self:
        cls = type(self)
        assignments = [(c, getattr(self, c)) for c in cls.__columns__]
        sql, params = native.compile_query(
            "insert",
            cls.__table__,
            assignments=assignments,
            returning=list(cls.__columns__),
        )
        rows = await current_database().fetch(sql, *params)
        self.refresh(cls.from_row(rows[0]))
        return self

    def refresh(self, stored: Model) -> None:
        for column in type(self).__columns__:
            setattr(self, column, getattr(stored, column))
        self.remember()

    def save(self) -> Self:
        return run_sync(self.asave())

    def related(self, name: str) -> Any:
        """The loaded relation ``name`` (``RelationNotLoaded`` if it was not)."""
        relation = self.__relations__.get(name)
        if relation is None:
            raise FieldError(f"{type(self).__name__} has no relation {name!r}")
        return getattr(self, name)

    @classmethod
    def filter(cls, **conditions: Any) -> QuerySet[Self]:
        return QuerySet(cls).filter(**conditions)

    @classmethod
    def all(cls) -> QuerySet[Self]:
        return QuerySet(cls)

    @classmethod
    async def acreate(cls, **values: Any) -> Self:
        """Insert a row (defaults and factories apply) and return it."""
        for name in values:
            cls.check_field(name)
        return await cls(**values).insert()

    @classmethod
    async def aget(cls, **conditions: Any) -> Self:
        """Exactly one row, else ``Model.NotFound`` / ``MultipleObjectsReturned``."""
        return await cls.filter(**conditions).aget()

    @classmethod
    async def aget_or_none(cls, **conditions: Any) -> Self | None:
        try:
            return await cls.aget(**conditions)
        except cls.NotFound:
            return None

    @classmethod
    async def aupdate(cls, **values: Any) -> Self:
        """Update by primary key (``id=...``) and return the updated row."""
        key = cls.__primary_key__
        if key not in values:
            raise FieldError(f"{cls.__name__}.aupdate needs {key}=...")
        identity = values.pop(key)
        if not values:
            raise FieldError("nothing to update")
        for name in values:
            cls.check_field(name)
        sql, params = native.compile_query(
            "update",
            cls.__table__,
            assignments=list(values.items()),
            filters=[(key, "eq", [identity])],
            returning=list(cls.__columns__),
        )
        rows = await current_database().fetch(sql, *params)
        if not rows:
            raise cls.NotFound(f"{cls.__name__} not found")
        return cls.from_row(rows[0])

    @classmethod
    async def adelete(cls, **conditions: Any) -> int:
        """Delete matching rows; returns how many. An empty condition set is
        refused so a missing filter cannot wipe the table."""
        if not conditions:
            raise FieldError("adelete needs at least one condition")
        query = QuerySet(cls).filter(**conditions)
        if query.loading.joined:
            raise FieldError(
                "adelete cannot filter across a relation; look the keys up first"
            )
        filters = list(query.filters)
        sql, params = native.compile_query("delete", cls.__table__, filters=filters)
        return await current_database().execute(sql, *params)

    @classmethod
    def create(cls, **values: Any) -> Self:
        """Sync ``acreate`` for sync handlers."""
        return run_sync(cls.acreate(**values))

    @classmethod
    def get(cls, **conditions: Any) -> Self:
        return run_sync(cls.aget(**conditions))

    @classmethod
    def get_or_none(cls, **conditions: Any) -> Self | None:
        return run_sync(cls.aget_or_none(**conditions))

    @classmethod
    def update(cls, **values: Any) -> Self:
        return run_sync(cls.aupdate(**values))

    @classmethod
    def delete(cls, **conditions: Any) -> int:
        return run_sync(cls.adelete(**conditions))

    @classmethod
    def check_field(cls, name: str) -> None:
        if name not in cls.__columns__:
            raise FieldError(f"{cls.__name__} has no field {name!r}")


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
        order: tuple[tuple[str, bool], ...] = (),
        limit_to: int | None = None,
        offset_by: int | None = None,
        loading: Loading = Loading(),  # noqa: B008 - frozen, safe to share
    ) -> None:
        self.model = model
        self.filters = filters
        self.order = order
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

    def joins(self) -> list[tuple[str, str, str, str, bool, list[str]]]:
        """Joins for selected relations (with columns) and filtered ones (without)."""
        names = dict.fromkeys((*self.loading.selected, *self.loading.joined))
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

    def compile(self, kind: str = "select") -> tuple[str, list[Any]]:
        return native.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=self.limit_to,
            offset=self.offset_by,
            joins=self.joins(),
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
            store(instance, relation.name, children.get(getattr(instance, key), []))

    async def aall(self) -> list[M]:
        sql, params = self.compile()
        rows = await current_database().fetch(sql, *params)
        instances = [self.build(row) for row in rows]
        await self.load(instances)
        return instances

    async def afirst(self) -> M | None:
        rows = await self.limit(1).aall()
        return rows[0] if rows else None

    async def aget(self) -> M:
        rows = await self.limit(2).aall()
        if not rows:
            raise self.model.NotFound(f"{self.model.__name__} not found")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"{self.model.__name__}: several rows match")
        return rows[0]

    async def acount(self) -> int:
        sql, params = self.derive(order=(), limit_to=None, offset_by=None).compile(
            "count"
        )
        rows = await current_database().fetch(sql, *params)
        return int(rows[0]["count"])

    async def aexists(self) -> bool:
        return await self.afirst() is not None

    def list(self) -> builtins.list[M]:
        """Sync ``aall`` for sync handlers."""
        return run_sync(self.aall())

    def first(self) -> M | None:
        return run_sync(self.afirst())

    def get(self) -> M:
        return run_sync(self.aget())

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
        return Page(await window.aall(), params.page, params.page_size, total)


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


async def fetch_in(model: type[Model], column: str, values: list[Any]) -> list[Any]:
    """Rows of ``model`` whose ``column`` is one of ``values`` (chunked so the
    statement stays far below PostgreSQL's parameter limit)."""
    found: list[Model] = []
    for start in range(0, len(values), IN_CHUNK):
        chunk = values[start : start + IN_CHUNK]
        found.extend(await QuerySet(model).filter(**{f"{column}__in": chunk}).aall())
    return found
