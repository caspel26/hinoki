"""Synthetic demo data for a disposable database (never use real people).

    BLOG_DATABASE_URL=postgres://... uv run python -m examples.blog.seed

Applies the migrations and inserts three users and twelve posts. The editor's
token is ``demo-editor-token`` (users ``ada`` and ``rae`` can also log in with
``demo-password-change-me`` when the app has ``BLOG_JWT_SECRET``); use it as
the ``blog_token`` cookie for /admin or as a bearer token for the API.
"""

import asyncio
import os
from pathlib import Path

from examples.blog.models import Post, User
from hinoki.crypto import PasswordHasher
from hinoki.db import Database
from hinoki.orm import migrator, use_database

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


async def seed(url: str) -> None:
    database = await Database.connect(url, max_size=2)
    hasher = PasswordHasher()
    try:
        await migrator.apply(database, MIGRATIONS)
        with use_database(database):
            hashed = await hasher.hash(DEMO_PASSWORD)
            ada = await User.acreate(
                name="Ada Editor",
                token="demo-editor-token",
                is_editor=True,
                username="ada",
                password_hash=hashed,
            )
            await User.acreate(
                name="Rae Reader",
                token="demo-reader-token",
                username="rae",
                password_hash=hashed,
            )
            await User.acreate(
                name="Bob Editor", token="demo-other-token", is_editor=True
            )
            for index, title in enumerate(TITLES):
                await Post.acreate(
                    author_id=ada.id,
                    title=title,
                    slug=f"post-{index + 1}",
                    body=f"Synthetic body for {title}.",
                    published=not title.startswith("Draft"),
                )
    finally:
        hasher.close()
        database.close()


if __name__ == "__main__":
    asyncio.run(seed(os.environ["BLOG_DATABASE_URL"]))
