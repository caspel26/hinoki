"""Database access: pooled, typed, with transactions bound to the task context.

``Database`` wraps the native engine. Create it inside a worker (a module
lifespan), never at import. Inside ``async with db.transaction():`` every
``db.fetch``/``db.execute`` of that task uses the transaction's connection;
nested scopes become savepoints; leaving the scope commits, or rolls back on
any exception (including cancellation and timeouts).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING

from hinoki import native
from hinoki.modules import Module, ModuleContext
from hinoki.native import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)

if TYPE_CHECKING:
    from hinoki.native import Param, Row

__all__ = [
    "CheckViolation",
    "Database",
    "DatabaseError",
    "ForeignKeyViolation",
    "NotNullViolation",
    "UniqueViolation",
]

log = logging.getLogger("hinoki.db")


class ActiveTransaction:
    """A native transaction plus its savepoint depth."""

    def __init__(self, database: Database, native_tx: native.Transaction) -> None:
        self.database = database
        self.native = native_tx
        self.depth = 0


current: ContextVar[ActiveTransaction | None] = ContextVar(
    "hinoki_transaction", default=None
)


class Database:
    """A connection pool for one PostgreSQL database."""

    def __init__(self, engine: native.Database) -> None:
        self.engine = engine

    @classmethod
    async def connect(
        cls, url: str, *, max_size: int = 8, wait: float = 5.0
    ) -> Database:
        """Open the pool and verify the server answers (fails fast at startup)."""
        engine = await native.Database.connect(url, max_size, int(wait * 1000))
        return cls(engine)

    def active(self) -> ActiveTransaction | None:
        found = current.get()
        return found if found is not None and found.database is self else None

    async def fetch(self, sql: str, *params: Param) -> list[Row]:
        """Rows as dicts. Placeholders are ``$1``, ``$2``, ..."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch(sql, list(params))

    async def fetch_one(self, sql: str, *params: Param) -> Row | None:
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    async def execute(self, sql: str, *params: Param) -> int:
        """Number of affected rows."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.execute(sql, list(params))

    def transaction(self) -> TransactionScope:
        return TransactionScope(self)

    def close(self) -> None:
        self.engine.close()

    def status(self) -> tuple[int, int]:
        """``(connections, idle)`` in the pool."""
        return self.engine.status()


class TransactionScope:
    """``async with db.transaction():`` — commit, rollback or savepoint."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.state: ActiveTransaction | None = None
        self.token: Token[ActiveTransaction | None] | None = None
        self.savepoint: str | None = None

    async def __aenter__(self) -> TransactionScope:
        parent = self.database.active()
        if parent is None:
            native_tx = await self.database.engine.begin()
            self.state = ActiveTransaction(self.database, native_tx)
            self.token = current.set(self.state)
            return self
        self.state = parent
        self.savepoint = f"hinoki_sp_{parent.depth}"
        await parent.native.execute(f"SAVEPOINT {self.savepoint}", [])
        parent.depth += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        state = self.state
        if state is None:
            raise RuntimeError("transaction scope was not entered")
        if self.savepoint is not None:
            await self.leave_savepoint(state, exc is None)
            return
        if self.token is not None:
            current.reset(self.token)
        if exc is None:
            await state.native.commit()  # a failed commit is an error, not success
        else:
            await self.rollback(state)

    async def leave_savepoint(self, state: ActiveTransaction, ok: bool) -> None:
        state.depth -= 1
        verb = "RELEASE SAVEPOINT" if ok else "ROLLBACK TO SAVEPOINT"
        await state.native.execute(f"{verb} {self.savepoint}", [])

    @staticmethod
    async def rollback(state: ActiveTransaction) -> None:
        """Roll back; if that fails the native side discards the connection."""
        try:
            await state.native.rollback()
        except Exception:
            log.exception("rollback failed; the connection will be discarded")


class DatabaseModule(Module):
    """Opens a ``Database`` for the worker and provides it to other modules.

    ``app.install(DatabaseModule(url))``; actions with ``transaction=True``
    and other modules obtain it with ``ctx.get(Database)``. The pool is
    created in the worker's lifespan and closed when it ends.
    """

    name = "database"

    def __init__(self, url: str, *, max_size: int = 8, wait: float = 5.0) -> None:
        self.url = url
        self.max_size = max_size
        self.wait = wait

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
