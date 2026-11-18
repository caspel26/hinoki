"""Database access: pooled, typed, with transactions bound to the task context.

``Database`` wraps the native engine. Create it inside a worker (a module
lifespan), never at import. Inside ``async with database.atomic():`` every
``fetch``/``execute`` of that task uses the transaction's connection; nested
scopes become savepoints; leaving the scope commits, or rolls back on any
exception (including cancellation and timeouts).

The namespace also carries the model declaration decorators, so a models
module reads ``from hinoki import db`` and then ``@db.table("posts")``,
``@db.indexed("created_at")`` and ``@db.ordered_by("-created_at")``. They are
the decorators of ``hinoki.orm.options`` (the same objects), loaded on first
access.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING

from hinoki import core
from hinoki.core import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from hinoki.modules import Module, ModuleContext

if TYPE_CHECKING:
    from hinoki.core import Param, Row, _RowPlan
    from hinoki.orm.options import indexed, ordered_by, table

__all__ = [
    "CheckViolation",
    "Database",
    "DatabaseError",
    "ForeignKeyViolation",
    "NotNullViolation",
    "UniqueViolation",
    "indexed",
    "ordered_by",
    "table",
]

log = logging.getLogger("hinoki.db")


# The model decorators are those of hinoki.orm.options, re-exported lazily:
# hinoki.orm imports this module (hinoki.orm.context), so a module-level import
# here would be circular, and importing hinoki.db should not load the ORM.
MODEL_DECORATORS = frozenset({"indexed", "ordered_by", "table"})

if not TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        if name in MODEL_DECORATORS:
            from hinoki.orm import options  # noqa: PLC0415 - circular, see above

            found: object = getattr(options, name)
            globals()[name] = found  # later lookups skip this function
            return found
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ActiveTransaction:
    """A native transaction, the task that owns it and its savepoint counter."""

    def __init__(self, database: Database, native_tx: core.Transaction) -> None:
        self.database = database
        self.native = native_tx
        self.owner = asyncio.current_task()
        self.savepoints = 0
        self.finished = False
        self.commit_callbacks: list[Callable[[], object]] = []

    def next_savepoint(self) -> str:
        """A name never reused within this transaction, so sibling scopes can
        not release or roll back each other's savepoints."""
        self.savepoints += 1
        return f"hinoki_sp_{self.savepoints}"


current: ContextVar[ActiveTransaction | None] = ContextVar(
    "hinoki_transaction", default=None
)


class Database:
    """A connection pool for one configured database backend."""

    def __init__(self, engine: core.Database) -> None:
        self.engine = engine

    @classmethod
    async def connect(
        cls, url: str, *, max_size: int = 8, wait: float = 5.0
    ) -> Database:
        """Open the pool and check the server; PostgreSQL URLs may configure TLS."""
        engine = await core.Database.connect(url, max_size, int(wait * 1000))
        return cls(engine)

    def active(self) -> ActiveTransaction | None:
        found = current.get()
        if found is None or found.database is not self:
            return None
        if found.finished:
            raise DatabaseError(
                "the transaction has ended; a task that outlives its "
                "atomic scope cannot use the database"
            )
        return found

    @property
    def dialect(self) -> str:
        """The selected dialect: ``postgres``, ``sqlite``, ``mysql`` or ``mariadb``."""
        return self.engine.dialect

    async def fetch(self, sql: str, *params: Param) -> list[Row]:
        """Rows as dicts, with placeholders selected by the active backend."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch(sql, list(params))

    async def _fetch_page_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        page: int,
        page_size: int,
        total: int,
    ) -> bytes | list[Row]:
        """Private generated-list fetch; fallback rows are already fetched."""
        active = self.active()
        target = active.native if active else self.engine
        return await target._fetch_page_wire(
            sql, params, plan, (page, page_size, total)
        )

    async def fetch_one(self, sql: str, *params: Param) -> Row | None:
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    async def execute(self, sql: str, *params: Param) -> int:
        """Number of affected rows; raw SQL placeholders follow the backend."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.execute(sql, list(params))

    def atomic(self) -> AtomicScope:
        """``async with database.atomic():`` — commit on success, roll back on
        any exception; inside an open scope of the same task, a savepoint."""
        return AtomicScope(self)

    def after_commit(self, callback: Callable[[], object]) -> None:
        """Run ``callback`` (sync or async) after the outermost transaction
        commits, in registration order. A rollback, of the transaction or of
        the savepoint scope it was registered in, drops it; an exception in a
        callback is logged and does not undo the commit."""
        active = self.active()
        if active is None:
            raise DatabaseError("after_commit needs an active transaction")
        active.commit_callbacks.append(callback)

    def migration(self) -> AtomicScope:
        """An exclusive migration transaction with backend locking semantics."""
        return AtomicScope(self, migration=True)

    def close(self) -> None:
        self.engine.close()

    def status(self) -> tuple[int, int]:
        """``(connections, idle)`` in the pool."""
        return self.engine.status()


class AtomicScope:
    """``async with database.atomic():`` — commit, rollback or savepoint."""

    def __init__(self, database: Database, *, migration: bool = False) -> None:
        self.database = database
        self.migration = migration
        self.state: ActiveTransaction | None = None
        self.token: Token[ActiveTransaction | None] | None = None
        self.savepoint: str | None = None
        self.callback_mark = 0

    async def __aenter__(self) -> AtomicScope:
        parent = self.database.active()
        if parent is None:
            native_tx = (
                await self.database.engine.begin_migration()
                if self.migration
                else await self.database.engine.begin()
            )
            self.state = ActiveTransaction(self.database, native_tx)
            self.token = current.set(self.state)
            return self
        if asyncio.current_task() is not parent.owner:
            raise DatabaseError(
                "nested atomic scopes (savepoints) must be opened by the "
                "task that owns the transaction"
            )
        if self.migration:
            raise DatabaseError("migration transactions cannot be nested")
        self.state = parent
        self.savepoint = parent.next_savepoint()
        self.callback_mark = len(parent.commit_callbacks)
        await parent.native.execute(f"SAVEPOINT {self.savepoint}", [])
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        state = self.state
        if state is None:
            raise RuntimeError("atomic scope was not entered")
        if self.savepoint is not None:
            await self.leave_savepoint(state, exc is None)
            return
        if self.token is not None:
            current.reset(self.token)
        state.finished = True
        if exc is not None:
            await self.rollback(state)
            return
        try:
            await state.native.commit()  # a failed commit is an error, not success
        except BaseException:
            await self.rollback(state)  # make sure nothing stays open
            raise
        await self.run_commit_callbacks(state)

    async def leave_savepoint(self, state: ActiveTransaction, ok: bool) -> None:
        verb = "RELEASE SAVEPOINT" if ok else "ROLLBACK TO SAVEPOINT"
        if not ok:
            del state.commit_callbacks[self.callback_mark :]
        await state.native.execute(f"{verb} {self.savepoint}", [])

    @staticmethod
    async def run_commit_callbacks(state: ActiveTransaction) -> None:
        for callback in state.commit_callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("an after_commit callback failed")

    @staticmethod
    async def rollback(state: ActiveTransaction) -> None:
        """Roll back; if that fails the native side discards the connection."""
        try:
            await state.native.rollback()
        except Exception:
            log.exception("rollback failed; the connection will be discarded")


class DatabaseModule(Module):
    """Opens a ``Database`` for the worker and provides it to other modules.

    ``app.install(DatabaseModule(url))``; actions with ``atomic=True``
    and other modules obtain it with ``ctx.get(Database)``. The pool is
    created in the worker's lifespan and closed when it ends.
    """

    name = "database"

    def __init__(self, url: str, *, max_size: int = 8, wait: float = 5.0) -> None:
        self.url = url
        self.max_size = max_size
        self.wait = wait

    @classmethod
    def from_settings(cls, settings: object) -> DatabaseModule:
        """The module for ``settings.database`` (a ``DatabaseSettings``: url and
        max_size), so ``Hinoki(settings=..., modules=[DatabaseModule])`` works."""
        group = getattr(settings, "database", None)
        if group is None or not hasattr(group, "url"):
            raise TypeError(
                "DatabaseModule needs a `database: DatabaseSettings` field in the "
                "application's settings"
            )
        return cls(group.url.reveal(), max_size=group.max_size)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        database = await Database.connect(
            self.url, max_size=self.max_size, wait=self.wait
        )
        ctx.provide(Database, database)
        try:
            yield
        finally:
            database.close()
