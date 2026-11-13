"""The active database for models: ``use_database`` and ``current_database``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from hinoki.db import Database

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
