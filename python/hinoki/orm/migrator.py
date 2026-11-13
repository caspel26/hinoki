"""Applying and inspecting migration files against a database.

Only called explicitly (``hinoki migrations apply``); the framework
never applies migrations at startup. Each file runs in one transaction under an
advisory lock and is recorded with its checksum, so an edited or missing
applied file is an error instead of silent drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hinoki.db import Database, DatabaseError
from hinoki.errors import MigrationError
from hinoki.orm.migrations import migration_files, statements

LOCK_KEY = 7266017
CREATE_LEDGER = (
    "CREATE TABLE IF NOT EXISTS hinoki_migrations ("
    "name text PRIMARY KEY, checksum text NOT NULL, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)
SELECT_LEDGER = "SELECT name, checksum FROM hinoki_migrations"
INSERT_LEDGER = "INSERT INTO hinoki_migrations (name, checksum) VALUES ($1, $2)"


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def recorded(database: Database) -> dict[str, str]:
    rows = await database.fetch(SELECT_LEDGER)
    return {row["name"]: row["checksum"] for row in rows}


async def status(database: Database, directory: Path) -> list[tuple[str, str]]:
    """``(name, state)`` per migration: ``applied``, ``pending``, ``changed``
    (applied, but the file differs) or ``missing`` (applied, file gone)."""
    await database.execute(CREATE_LEDGER)
    applied = await recorded(database)
    files = {p.stem: p for p in migration_files(directory)}
    result: list[tuple[str, str]] = []
    for name in sorted(files.keys() | applied.keys()):
        if name not in files:
            result.append((name, "missing"))
        elif name not in applied:
            result.append((name, "pending"))
        elif applied[name] != checksum(files[name]):
            result.append((name, "changed"))
        else:
            result.append((name, "applied"))
    return result


async def apply(database: Database, directory: Path) -> list[str]:
    """Apply pending migrations in order; returns the names applied."""
    problems = [
        f"{n} is {s}"
        for n, s in await status(database, directory)
        if s in {"changed", "missing"}
    ]
    if problems:
        raise MigrationError("; ".join(problems))
    return [
        path.stem
        for path in migration_files(directory)
        if await apply_one(database, path)
    ]


async def apply_one(database: Database, path: Path) -> bool:
    """One file, one transaction. False when it was already applied."""
    async with database.transaction():
        await database.fetch("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        if path.stem in await recorded(database):
            return False
        try:
            for statement in statements(path.read_text(encoding="utf-8")):
                await database.execute(statement)
        except DatabaseError as error:
            raise MigrationError(f"{path.stem} failed and was rolled back") from error
        await database.execute(
            INSERT_LEDGER,
            path.stem,
            checksum(path),
        )
    return True
