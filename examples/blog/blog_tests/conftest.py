"""A disposable SQLite database per test: blog and admin migrations, two tenants."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from blog.apps.blog import models
from blog.apps.blog.security import hash_token
from blog.settings import BlogSettings
from blogworld import ADMIN_SECRET, JWT_SECRET, PASSWORD, World

from hinoki import ratelimit_store
from hinoki.admin.users import migrations as admin_migrations
from hinoki.crypto import PasswordHasher
from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.testing import ASGIClient

# ``blog.main`` builds the module-level app on import, and the admin refuses to
# start without a secret: give it a synthetic one before anything imports it.
os.environ.setdefault("BLOG_ADMIN__SECRET", ADMIN_SECRET)

BLOG_MIGRATIONS = Path(models.__file__).parent / "migrations"
ADMIN_MIGRATIONS = Path(admin_migrations.__file__).parent
LIMIT_MIGRATIONS = Path(ratelimit_store.__file__).parent / "migrations"


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    url = f"sqlite://{tmp_path / 'blog.sqlite3'}"
    database = await Database.connect(url, max_size=1)
    # The blog's app name is its root package, so its migrations have no namespace.
    await migrator.apply(database, BLOG_MIGRATIONS, namespace="")
    await migrator.apply(database, ADMIN_MIGRATIONS, namespace="admin")
    await migrator.apply(database, LIMIT_MIGRATIONS, namespace="ratelimit")
    hasher = PasswordHasher()
    with use_database(database):
        press = await models.Publication.acreate(name="Press", slug="press")
        cedar = await models.Publication.acreate(name="Cedar", slug="cedar")
        ada = await models.User.acreate(
            publication_id=press.id,
            name="Ada",
            token_hash=hash_token("ada-token"),
            is_editor=True,
            username="ada",
            password_hash=await hasher.hash(PASSWORD),
        )
        await models.User.acreate(
            publication_id=press.id, name="Rae", token_hash=hash_token("rae-token")
        )
        await models.User.acreate(
            publication_id=press.id,
            name="NoPassword",
            token_hash=hash_token("np"),
            username="nopass",
        )
        bob = await models.User.acreate(
            publication_id=cedar.id,
            name="Bob",
            token_hash=hash_token("bob-token"),
            is_editor=True,
        )
        await models.Post.acreate(
            publication_id=press.id,
            author_id=ada.id,
            title="Live",
            slug="live",
            body="b",
            published=True,
        )
        await models.Post.acreate(
            publication_id=press.id,
            author_id=ada.id,
            title="Draft",
            slug="draft",
            body="b",
        )
        await models.Post.acreate(
            publication_id=cedar.id,
            author_id=bob.id,
            title="Cedar draft",
            slug="cedar-draft",
            body="b",
        )
    hasher.close()
    database.close()
    from blog.main import build_app  # noqa: PLC0415 - after the secret is set above

    settings = BlogSettings.load(
        overrides={
            "database": {"url": url},
            "admin": {"secret": ADMIN_SECRET},
            "jwt_secret": JWT_SECRET,
        }
    )
    async with ASGIClient(build_app(settings)) as client:
        yield World(client, ada.id, url)
