"""Synthetic demo data for a disposable database (never use real people).

BLOG_DATABASE__URL=postgres://... uv run python -m blog.apps.blog.seed

Applies the blog's migrations and inserts two publications, three users and twelve
posts (the first publication's). Each user gets a random bearer token, printed
once (only its SHA-256 digest is stored); users ``ada`` and ``rae`` can also log
in with ``demo-password-change-me`` when the app has ``BLOG_JWT_SECRET``.
The admin has its own sign-in: create a superuser with
``hinoki admin create-superuser`` (see the README).
"""

import asyncio
from pathlib import Path

from hinoki.crypto import PasswordHasher, random_token
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.settings import DatabaseSettings

from .models import Post, Publication, User
from .security import hash_token

MIGRATIONS = Path(__file__).parent / "migrations"
DEMO_PASSWORD = "demo-password-change-me"  # noqa: S105 - synthetic; the demo users are fake
TITLES = [
    "Getting started with Hinoki",
    "Typed handlers, plain Python",
    "Why the request is optional",
    "Policies that read like prose",
    "Transactions without surprises",
    "Pagination you can reason about",
    "Relations that never lazy-load",
    "Migrations you can read",
    "Rules for the team, not the tool",
    "Notes on <b>escaping</b> output",
    "Draft: native parsing",
    "Draft: an admin for editors",
]


async def seed(url: str) -> dict[str, str]:
    """Seed the database and return each demo user's name and bearer token."""
    tokens = {name: random_token() for name in ("ada", "rae", "bob")}
    database = await Database.connect(url, max_size=2)
    hasher = PasswordHasher()
    try:
        await migrator.apply(database, MIGRATIONS)
        with use_database(database):
            hashed = await hasher.hash(DEMO_PASSWORD)
            press = await Publication.acreate(name="Hinoki Press", slug="hinoki-press")
            other = await Publication.acreate(name="Cedar Weekly", slug="cedar-weekly")
            ada = await User.acreate(
                publication_id=press.id,
                name="Ada Editor",
                token_hash=hash_token(tokens["ada"]),
                is_editor=True,
                username="ada",
                password_hash=hashed,
            )
            await User.acreate(
                publication_id=press.id,
                name="Rae Reader",
                token_hash=hash_token(tokens["rae"]),
                username="rae",
                password_hash=hashed,
            )
            await User.acreate(
                publication_id=other.id,
                name="Bob Editor",
                token_hash=hash_token(tokens["bob"]),
                is_editor=True,
            )
            for index, title in enumerate(TITLES):
                await Post.acreate(
                    publication_id=press.id,
                    author_id=ada.id,
                    title=title,
                    slug=f"post-{index + 1}",
                    body=f"Synthetic body for {title}.",
                    published=not title.startswith("Draft"),
                )
    finally:
        hasher.close()
        database.close()
    return tokens


if __name__ == "__main__":
    database_settings = DatabaseSettings.load(prefix="BLOG_DATABASE__")
    for user, token in asyncio.run(seed(database_settings.url.reveal())).items():
        print(f"{user}: {token}")
