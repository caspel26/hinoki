"""Database access: pooled, typed, with transactions bound to the task context.

``Database`` wraps the native engine. Create it inside a worker (a module
lifespan), never at import. Inside ``async with database.atomic():`` every
``fetch``/``execute`` of that task uses the transaction's connection; nested
scopes become savepoints; leaving the scope commits, or rolls back on any
exception (including cancellation and timeouts).

``db.atomic`` opens the same scope on the current database (the one a request
or ``use_database`` made active): ``async with db.atomic():`` for a block, or
``@db.atomic`` / ``@db.atomic()`` on an ``async def`` for a whole call.

The namespace also carries the model declaration decorators, so a models
module reads ``from hinoki import db`` and then ``@db.table("posts")``,
``@db.unique("author_id", "slug")``, ``@db.indexed("created_at")``,
``@db.ordered_by("-created_at")`` and ``@db.timestamps()``. They are the
decorators of ``hinoki.orm.options`` (the same objects), loaded on first
access.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import ipaddress
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING, Any, overload
from urllib.parse import parse_qsl, urlsplit

from hinoki import core
from hinoki.blocking import run_sync
from hinoki.core import (
    CheckViolation,
    DatabaseError,
    ForeignKeyViolation,
    NotNullViolation,
    UniqueViolation,
)
from hinoki.modules import Module, ModuleContext, Resource

if TYPE_CHECKING:
    from hinoki.core import (
        Param,
        Row,
        _CursorCodec,
        _ModelRows,
        _ProjectedRows,
        _RowPlan,
    )
    from hinoki.orm.options import (
        indexed,
        ordered_by,
        soft_delete,
        table,
        timestamps,
        track_status,
        unique,
    )

__all__ = [
    "Atomic",
    "AtomicScope",
    "CheckViolation",
    "Database",
    "DatabaseError",
    "DatabaseModule",
    "ForeignKeyViolation",
    "NotNullViolation",
    "UniqueViolation",
    "atomic",
    "indexed",
    "ordered_by",
    "soft_delete",
    "table",
    "timestamps",
    "track_status",
    "unique",
]

log = logging.getLogger("hinoki.db")


# The model decorators are those of hinoki.orm.options, re-exported lazily:
# hinoki.orm imports this module (hinoki.orm.context), so a module-level import
# here would be circular, and importing hinoki.db should not load the ORM.
MODEL_DECORATORS = frozenset(
    {
        "indexed",
        "ordered_by",
        "soft_delete",
        "table",
        "timestamps",
        "track_status",
        "unique",
    }
)

if not TYPE_CHECKING:

    def __getattr__(name: str) -> object:
        if name in MODEL_DECORATORS:
            from hinoki.orm import options  # noqa: PLC0415 - circular, see above

            found: object = getattr(options, name)
            globals()[name] = found  # later lookups skip this function
            return found
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


NATIVE_OWNER = object()


def current_owner() -> object:
    """Who owns a transaction opened now: the running task, or, in a coroutine
    Rust drives (which has no task), one marker; each request runs in its own
    context, so the transaction variable never crosses requests."""
    if core.driving():
        return NATIVE_OWNER
    return asyncio.current_task()


class ActiveTransaction:
    """A native transaction, the task that owns it and its savepoint counter."""

    def __init__(self, database: Database, native_tx: core.Transaction) -> None:
        self.database = database
        self.native = native_tx
        self.owner = current_owner()
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


def warn_unverified_tls(url: str) -> None:
    """Warn when a PostgreSQL URL to a remote host leaves ``sslmode`` at its
    default (``prefer``): a network attacker can strip TLS or impersonate the
    server. The URL is not logged, it holds the password."""
    if not url.startswith(("postgres://", "postgresql://")):
        return
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return
    if any(key == "sslmode" for key, _ in parse_qsl(parts.query)):
        return
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host in {"", "localhost"} or host.lower().startswith(("/", "%2f"))
    if not local:
        log.warning(
            "database host %r is remote and sslmode is not set (it defaults to "
            "prefer, which a network attacker can downgrade); use "
            "sslmode=verify-full",
            host,
        )


class Database(Resource):
    """A connection pool for one configured database backend."""

    def __init__(self, engine: core.Database) -> None:
        self.engine = engine

    @classmethod
    async def connect(
        cls, url: str, *, max_size: int = 10, wait: float = 5.0
    ) -> Database:
        """Open the pool and check the server; PostgreSQL URLs may configure TLS."""
        warn_unverified_tls(url)
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

    async def fetch_models(
        self, sql: str, params: list[Any], plan: _ModelRows
    ) -> list[Any]:
        """Rows built into finished model instances (private; ``QuerySet.fetch``)."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_models(sql, list(params), plan)

    async def fetch_page_models(
        self,
        rows_sql: str,
        rows_params: list[Any],
        count_sql: str,
        count_params: list[Any],
        plan: _ModelRows,
        *,
        concurrent: bool = False,
    ) -> tuple[list[Any], int]:
        """Fetch one page of model instances and its total count (private)."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_page_models(
            rows_sql,
            list(rows_params),
            count_sql,
            list(count_params),
            plan,
            concurrent,
        )

    async def fetch_page_rows(
        self,
        rows_sql: str,
        rows_params: list[Any],
        count_sql: str,
        count_params: list[Any],
        *,
        concurrent: bool = False,
    ) -> tuple[list[Row], int]:
        """Fetch one page of raw rows and its total count (private)."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_page_rows(
            rows_sql,
            list(rows_params),
            count_sql,
            list(count_params),
            concurrent,
        )

    async def fetch_projected_list(
        self,
        sql: str,
        params: list[Any],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row]:
        """Fetch rows pre-encoded to JSON by Rust, or raw rows on fallback."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_projected_list(
            sql, params if type(params) is list else list(params), plan
        )

    async def fetch_projected_one(
        self,
        sql: str,
        params: list[Any],
        plan: _RowPlan,
    ) -> _ProjectedRows | list[Row] | str | None:
        """Fetch one row pre-encoded to JSON by Rust, ``None`` if empty,
        ``"multiple"`` if >1 row, or raw rows on fallback."""
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_projected_one(
            sql, params if type(params) is list else list(params), plan
        )

    async def fetch_projected_page(
        self,
        rows_sql: str,
        rows_params: list[Any],
        count_sql: str,
        count_params: list[Any],
        plan: _RowPlan,
        page_window: tuple[int, int],
        *,
        concurrent: bool = False,
    ) -> _ProjectedRows | tuple[list[Row], int]:
        """Fetch one page pre-encoded to JSON by Rust, or raw rows on fallback."""
        page, page_size = page_window
        active = self.active()
        target = active.native if active else self.engine
        return await target.fetch_projected_page(
            rows_sql,
            rows_params if type(rows_params) is list else list(rows_params),
            count_sql,
            count_params if type(count_params) is list else list(count_params),
            plan,
            page,
            page_size,
            concurrent,
        )

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

    async def _fetch_cursor_wire(
        self,
        sql: str,
        params: list[Param],
        plan: _RowPlan,
        codec: _CursorCodec,
        keys: list[str],
        limit: int,
        mode: int,
    ) -> bytes | tuple[list[Row], bool]:
        """Private keyset-list fetch: the page JSON, or the rows (in page order,
        cut to ``limit``) and whether more exist when Rust cannot encode them."""
        active = self.active()
        target = active.native if active else self.engine
        return await target._fetch_cursor_wire(
            sql, params, plan, codec, keys, limit, mode
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
        await self.open(shared=False)
        if self.savepoint is None:
            self.token = current.set(self.state)
        return self

    def __enter__(self) -> AtomicScope:
        """The same scope for a sync handler on a worker thread. The calls run
        on the worker's loop through ``run_sync``, whose tasks get a copy of the
        thread's context, so the transaction is made current here, on the thread.
        The thread blocks on every call, so a nested scope may not be opened by
        another task: it is a savepoint of the open transaction."""
        run_sync(self.open(shared=True))
        if self.savepoint is None:
            self.token = current.set(self.state)
        return self

    async def open(self, *, shared: bool) -> None:
        """Begin the transaction, or a savepoint inside the open one. ``shared``
        lets a scope opened by a different task of a sync handler's thread nest."""
        parent = self.database.active()
        if parent is None:
            native_tx = (
                await self.database.engine.begin_migration()
                if self.migration
                else await self.database.engine.begin()
            )
            self.state = ActiveTransaction(self.database, native_tx)
            return
        if not shared and current_owner() is not parent.owner:
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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.token is not None:
            current.reset(self.token)
        await self.close(exc)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.token is not None:
            current.reset(self.token)
        run_sync(self.close(exc))

    async def close(self, exc: BaseException | None) -> None:
        state = self.state
        if state is None:
            raise RuntimeError("atomic scope was not entered")
        if self.savepoint is not None:
            await self.leave_savepoint(state, exc is None)
            return
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
        """Release the savepoint, or roll back to it and then release it: a
        ``ROLLBACK TO`` keeps the savepoint open. A failed release counts as a
        rollback, so the block's ``after_commit`` callbacks are dropped."""
        if ok:
            try:
                await state.native.execute(f"RELEASE SAVEPOINT {self.savepoint}", [])
            except BaseException:
                del state.commit_callbacks[self.callback_mark :]
                await self.undo_savepoint(state)
                raise
            return
        del state.commit_callbacks[self.callback_mark :]
        await state.native.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}", [])
        await state.native.execute(f"RELEASE SAVEPOINT {self.savepoint}", [])

    async def undo_savepoint(self, state: ActiveTransaction) -> None:
        """Best effort after a failed release; the release error propagates."""
        try:
            await state.native.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}", [])
            await state.native.execute(f"RELEASE SAVEPOINT {self.savepoint}", [])
        except Exception:
            log.exception("rolling back savepoint %s failed", self.savepoint)

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


# The scopes ``db.atomic()`` blocks have open in this task or thread, innermost
# last. Blocks exit in reverse order of entry, so one module-level stack serves
# every Atomic object, including one reused by concurrent tasks.
open_scopes: ContextVar[tuple[AtomicScope, ...]] = ContextVar(
    "hinoki_atomic_scopes", default=()
)


class Atomic:
    """``db.atomic()``: an atomic scope on the current database, as a block or
    as a decorator.

    ``async with db.atomic():`` opens ``current_database().atomic()`` when the
    block is entered; ``with db.atomic():`` does the same in a sync handler.
    Decorating an ``async def`` or a plain ``def`` opens a fresh scope for every
    call: returning commits, an exception rolls back, and a call made inside an
    already atomic scope becomes a savepoint.
    """

    async def __aenter__(self) -> AtomicScope:
        scope = current_scope()
        await scope.__aenter__()
        open_scopes.set((*open_scopes.get(), scope))
        return scope

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.leave().__aexit__(exc_type, exc, tb)

    def __enter__(self) -> AtomicScope:
        scope = current_scope().__enter__()
        open_scopes.set((*open_scopes.get(), scope))
        return scope

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.leave().__exit__(exc_type, exc, tb)

    def leave(self) -> AtomicScope:
        """Pop this task's innermost open scope."""
        stack = open_scopes.get()
        if not stack:
            raise RuntimeError("atomic scope was not entered")
        open_scopes.set(stack[:-1])
        return stack[-1]

    @overload
    def __call__[**P, R](
        self, func: Callable[P, Coroutine[Any, Any, R]]
    ) -> Callable[P, Coroutine[Any, Any, R]]: ...
    @overload
    def __call__[**P, R](self, func: Callable[P, R]) -> Callable[P, R]: ...
    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def atomic_call(*args: Any, **kwargs: Any) -> Any:
                async with current_scope():
                    return await func(*args, **kwargs)

            return atomic_call

        @functools.wraps(func)
        def sync_call(*args: Any, **kwargs: Any) -> Any:
            with current_scope():
                return func(*args, **kwargs)

        return sync_call


def current_scope() -> AtomicScope:
    """A new atomic scope on the database that is current for this task."""
    # hinoki.orm.context imports this module; importing it at the top would be
    # circular, and using hinoki.db must not load the ORM.
    from hinoki.orm.context import current_database  # noqa: PLC0415

    return current_database().atomic()


@overload
def atomic[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]], /
) -> Callable[P, Coroutine[Any, Any, R]]: ...
@overload
def atomic[**P, R](func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def atomic() -> Atomic: ...
def atomic(func: Callable[..., Any] | None = None, /) -> Any:
    """``async with db.atomic():``, ``with db.atomic():``, ``@db.atomic`` or
    ``@db.atomic()``: run a block or a function, ``async def`` or plain ``def``,
    atomically on the current database. The sync forms are for sync handlers."""
    if func is None:
        return Atomic()
    return Atomic()(func)


class DatabaseModule(Module):
    """Opens a ``Database`` for the worker and provides it to other modules.

    ``app.install(DatabaseModule(url))``; actions with ``atomic=True``
    and other modules obtain it with ``ctx.get(Database)``. The pool is
    created in the worker's lifespan and closed when it ends.
    """

    name = "database"

    def __init__(self, url: str, *, max_size: int = 10, wait: float = 5.0) -> None:
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
