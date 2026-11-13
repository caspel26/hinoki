"""Resets the disposable database to the blog's schema using its migrations."""

from pathlib import Path

from hinoki.db import Database
from hinoki.orm import migrator

MIGRATIONS = Path(__file__).parent.parent / "examples" / "blog" / "migrations"
DROP = (
    "DROP TABLE IF EXISTS blog_posts",
    "DROP TABLE IF EXISTS blog_users",
    "DROP TABLE IF EXISTS hinoki_migrations",
)


async def reset(database: Database) -> None:
    for statement in DROP:
        await database.execute(statement)
    await migrator.apply(database, MIGRATIONS)


async def drop(database: Database) -> None:
    for statement in DROP:
        await database.execute(statement)
