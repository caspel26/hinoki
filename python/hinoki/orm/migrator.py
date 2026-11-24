"""Applying and inspecting migration files against a database.

Only called explicitly (``hinoki migrations apply``); the framework
never applies migrations at startup. Each file runs under a backend migration
lock and is recorded with its checksum, so an edited or missing applied file
is an error instead of silent drift. Transactional DDL is backend-dependent.
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
CREATE_SQLITE_LEDGER = (
    "CREATE TABLE IF NOT EXISTS hinoki_migrations ("
    "name text PRIMARY KEY, checksum text NOT NULL, "
    "applied_at text NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)
CREATE_MYSQL_LEDGER = (
    "CREATE TABLE IF NOT EXISTS hinoki_migrations ("
    "name varchar(255) PRIMARY KEY, checksum char(64) NOT NULL, "
    "applied_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB"
)
SELECT_LEDGER = "SELECT name, checksum FROM hinoki_migrations"
LEDGER_EXISTS = {
    "sqlite": (
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'hinoki_migrations'"
    ),
    "mysql": (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'hinoki_migrations'"
    ),
    "postgres": (
        "SELECT 1 AS present WHERE to_regclass('hinoki_migrations') IS NOT NULL"
    ),
}
INSERT_LEDGER = "INSERT INTO hinoki_migrations (name, checksum) VALUES ($1, $2)"
INSERT_SQLITE_LEDGER = "INSERT INTO hinoki_migrations (name, checksum) VALUES (?1, ?2)"
INSERT_MYSQL_LEDGER = "INSERT INTO hinoki_migrations (name, checksum) VALUES (?, ?)"


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def recorded(database: Database, namespace: str = "") -> dict[str, str]:
    rows = await database.fetch(SELECT_LEDGER)
    prefix = f"{namespace}:" if namespace else ""
    return {
        row["name"][len(prefix) :]: row["checksum"]
        for row in rows
        if (row["name"].startswith(prefix) if namespace else ":" not in row["name"])
    }


async def ensure_ledger(database: Database) -> None:
    await database.execute(
        CREATE_SQLITE_LEDGER
        if database.dialect == "sqlite"
        else CREATE_MYSQL_LEDGER
        if database.dialect in {"mysql", "mariadb"}
        else CREATE_LEDGER
    )


async def ledger_exists(database: Database) -> bool:
    dialect = "mysql" if database.dialect == "mariadb" else database.dialect
    return bool(await database.fetch(LEDGER_EXISTS[dialect]))


async def status(
    database: Database, directory: Path, *, namespace: str = ""
) -> list[tuple[str, str]]:
    """``(name, state)`` per migration: ``applied``, ``pending``, ``changed``
    (applied, but the file differs) or ``missing`` (applied, file gone).
    Read-only: without a ledger table nothing has been applied."""
    applied = (
        await recorded(database, namespace) if await ledger_exists(database) else {}
    )
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


async def apply(
    database: Database, directory: Path, *, namespace: str = ""
) -> list[str]:
    """Apply pending migrations in order; returns the names applied."""
    await ensure_ledger(database)
    problems = [
        f"{n} is {s}"
        for n, s in await status(database, directory, namespace=namespace)
        if s in {"changed", "missing"}
    ]
    if problems:
        raise MigrationError("; ".join(problems))
    return [
        path.stem
        for path in migration_files(directory)
        if await apply_one(database, path, namespace=namespace)
    ]


async def apply_one(database: Database, path: Path, *, namespace: str = "") -> bool:
    """One file, one transaction. False when it was already applied."""
    async with database.migration():
        if database.dialect == "postgres":
            await database.fetch("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        applied = await recorded(database, namespace)
        if path.stem in applied:
            return False
        try:
            for statement in statements(path.read_text(encoding="utf-8")):
                await database.execute(statement)
        except DatabaseError as error:
            detail = (
                "failed; DDL may be partially applied and was not rolled back"
                if database.dialect in {"mysql", "mariadb"}
                else "failed and was rolled back"
            )
            raise MigrationError(f"{path.stem} {detail}") from error
        if database.dialect == "sqlite" and await database.fetch(
            "PRAGMA foreign_key_check"
        ):
            raise MigrationError(
                f"{path.stem} violates SQLite foreign key constraints "
                "and was rolled back"
            )
        await database.execute(
            INSERT_SQLITE_LEDGER
            if database.dialect == "sqlite"
            else INSERT_MYSQL_LEDGER
            if database.dialect in {"mysql", "mariadb"}
            else INSERT_LEDGER,
            f"{namespace}:{path.stem}" if namespace else path.stem,
            checksum(path),
        )
    return True
