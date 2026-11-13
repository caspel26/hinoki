"""Active Record models over the Rust query compiler.

First slice: single-table models with equality/comparison/membership filters,
ordering, counting and pagination, plus create/get/update/delete. Relations,
migrations and sync variants come later. SQL is produced by the native
compiler from validated field names; values are always bound parameters.

Typing note: construction (``Post(title=...)``) is checked statically;
keyword-style helpers such as ``Post.acreate(**values)`` are not (see
docs/TYPING.md) until generated stubs exist.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar, Self, cast, dataclass_transform

from hinoki import native
from hinoki.db import Database
from hinoki.errors import (
    FieldError,
    MultipleObjectsReturned,
    ObjectNotFound,
    RegistrationError,
)
from hinoki.pagination import Page, PageParams

OPERATORS = {"gt", "gte", "lt", "lte", "ne", "in", "isnull"}
SEPARATOR = "__"
MARK = "hinoki_field"

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


@dataclass_transform(kw_only_default=True, field_specifiers=(field,))
class Model:
    """Base class: ``class Post(Model, table="posts"): ...``."""

    __table__: ClassVar[str] = ""
    __columns__: ClassVar[tuple[str, ...]] = ()
    __primary_key__: ClassVar[str] = ""
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
        cls.NotFound = type(
            "NotFound", (ObjectNotFound,), {"__qualname__": f"{cls.__name__}.NotFound"}
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        return cls(**row)

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
        instance = cls(**values)
        assignments = [(c, getattr(instance, c)) for c in cls.__columns__]
        sql, params = native.compile_query(
            "insert",
            cls.__table__,
            assignments=assignments,
            returning=list(cls.__columns__),
        )
        rows = await current_database().fetch(sql, *params)
        return cls.from_row(rows[0])

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
        filters = list(QuerySet(cls).filter(**conditions).filters)
        sql, params = native.compile_query("delete", cls.__table__, filters=filters)
        return await current_database().execute(sql, *params)

    @classmethod
    def check_field(cls, name: str) -> None:
        if name not in cls.__columns__:
            raise FieldError(f"{cls.__name__} has no field {name!r}")


class QuerySet[M: Model]:
    """Lazy, immutable query description; nothing runs until an ``a...`` call."""

    def __init__(
        self,
        model: type[M],
        filters: tuple[tuple[str, str, list[Any]], ...] = (),
        order: tuple[tuple[str, bool], ...] = (),
        limit_to: int | None = None,
        offset_by: int | None = None,
    ) -> None:
        self.model = model
        self.filters = filters
        self.order = order
        self.limit_to = limit_to
        self.offset_by = offset_by

    def derive(self, **changes: Any) -> QuerySet[M]:
        state = {
            "filters": self.filters,
            "order": self.order,
            "limit_to": self.limit_to,
            "offset_by": self.offset_by,
        }
        return QuerySet(self.model, **{**state, **changes})

    def filter(self, **conditions: Any) -> QuerySet[M]:
        added = tuple(self.condition(key, value) for key, value in conditions.items())
        return self.derive(filters=(*self.filters, *added))

    def condition(self, key: str, value: Any) -> tuple[str, str, list[Any]]:
        name, _, suffix = key.rpartition(SEPARATOR)
        if not name or suffix not in OPERATORS:
            name, suffix = key, "eq"
        self.model.check_field(name)
        if suffix == "in":
            return name, "in", list(value)
        if suffix == "isnull":
            return name, "isnull" if value else "notnull", []
        if value is None:
            if suffix not in {"eq", "ne"}:
                raise FieldError(f"{key} cannot compare with None")
            return name, "isnull" if suffix == "eq" else "notnull", []
        return name, suffix, [value]

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

    def compile(self, kind: str = "select") -> tuple[str, list[Any]]:
        return native.compile_query(
            kind,
            self.model.__table__,
            columns=list(self.model.__columns__),
            filters=list(self.filters),
            order=list(self.order),
            limit=self.limit_to,
            offset=self.offset_by,
        )

    async def aall(self) -> list[M]:
        sql, params = self.compile()
        rows = await current_database().fetch(sql, *params)
        return [self.model.from_row(row) for row in rows]

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

    async def apaginate(self, params: PageParams) -> Page[M]:
        """One page plus the total. Without ``order_by`` the primary key orders
        the rows so pages are stable."""
        ordered = self if self.order else self.order_by(self.model.__primary_key__)
        total = await self.acount()
        window = ordered.limit(params.page_size).offset(params.offset)
        return Page(await window.aall(), params.page, params.page_size, total)
