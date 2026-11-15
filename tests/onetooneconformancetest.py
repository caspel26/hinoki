"""One-to-one schema and relation behavior on every supported DB server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki.db import Database, UniqueViolation
from hinoki.orm import Model, OnDelete, field, foreign_key, use_database
from hinoki.orm.migrations import plan
from hinoki.orm.migrator import apply
from hinoki.orm.relations import Related

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")


class OneOwner(Model, table="one_to_one_owners"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    profile: ClassVar[Related[OneProfile | None]]
    optional_profile: ClassVar[Related[OneOptionalProfile | None]]


class OneProfile(Model, table="one_to_one_profiles"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(
        OneOwner,
        on_delete=OnDelete.CASCADE,
        related_name="profile",
        unique=True,
    )
    name: str
    owner: ClassVar[Related[OneOwner]]


class OneOptionalProfile(Model, table="one_to_one_optional_profiles"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID | None = foreign_key(
        OneOwner,
        on_delete=OnDelete.SET_NULL,
        related_name="optional_profile",
        unique=True,
        default=None,
    )
    name: str
    owner: ClassVar[Related[OneOwner | None]]


DATABASES = (
    pytest.param(None, "sqlite", id="sqlite"),
    pytest.param(
        POSTGRES_URL,
        "postgres",
        marks=pytest.mark.skipif(
            POSTGRES_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
        ),
        id="postgresql",
    ),
    pytest.param(
        MYSQL_URL,
        "mysql",
        marks=pytest.mark.skipif(
            MYSQL_URL is None, reason="HINOKI_TEST_MYSQL_URL is not set"
        ),
        id="mysql",
    ),
    pytest.param(
        MARIADB_URL,
        "mariadb",
        marks=pytest.mark.skipif(
            MARIADB_URL is None, reason="HINOKI_TEST_MARIADB_URL is not set"
        ),
        id="mariadb",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "dialect"), DATABASES)
async def test_unique_foreign_key_migration_and_prefetch(
    url: str | None, dialect: str, tmp_path: Path
) -> None:
    if dialect == "sqlite":
        url = f"sqlite://{tmp_path / 'onetoone.sqlite3'}"
    assert url is not None
    linked_id: UUID | None = None
    try:
        database = await Database.connect(url, max_size=2)
        try:
            await clear_one_to_one_tables(database)
            plan(
                [OneOwner, OneProfile, OneOptionalProfile],
                tmp_path,
                "initial",
                dialect=dialect,
            )
            assert await apply(database, tmp_path) == ["0001-initial"]
            assert await apply(database, tmp_path) == []
            linked_id = await check_one_to_one_behavior(database)
        finally:
            database.close()
        assert linked_id is not None
        cleanup = await Database.connect(url, max_size=1)
        try:
            with use_database(cleanup), pytest.raises(UniqueViolation):
                await OneProfile.acreate(owner_id=linked_id, name="duplicate")
        finally:
            cleanup.close()
    finally:
        cleanup = await Database.connect(url, max_size=1)
        try:
            await clear_one_to_one_tables(cleanup)
        finally:
            cleanup.close()


async def clear_one_to_one_tables(database: Database) -> None:
    for table in (
        "one_to_one_profiles",
        "one_to_one_optional_profiles",
        "one_to_one_owners",
        "hinoki_migrations",
    ):
        await database.execute(f"DROP TABLE IF EXISTS {table}")


async def check_one_to_one_behavior(database: Database) -> UUID:
    """Exercise eager loading, nullable reverse links and ON DELETE behavior."""
    with use_database(database):
        linked = await OneOwner.acreate(name="linked")
        empty = await OneOwner.acreate(name="empty")
        profile = await OneProfile.acreate(owner_id=linked.id, name="profile")
        optional = await OneOptionalProfile.acreate(owner_id=empty.id, name="optional")
        await OneOptionalProfile.acreate(owner_id=None, name="free one")
        await OneOptionalProfile.acreate(owner_id=None, name="free two")

        owners = (
            await OneOwner.all()
            .order_by("name")
            .prefetch_related("profile", "optional_profile")
        )
        assert [owner.name for owner in owners] == ["empty", "linked"]
        assert [owner.profile for owner in owners] == [None, profile]
        assert [owner.optional_profile for owner in owners] == [optional, None]
        [loaded] = await OneProfile.all().select_related("owner")
        assert loaded.owner.id == linked.id
        free_profiles = await OneOptionalProfile.filter(
            owner_id__isnull=True
        ).select_related("owner")
        assert len(free_profiles) == 2
        assert all(item.owner is None for item in free_profiles)

        await OneOwner.adelete(id=empty.id)
        [cleared] = await OneOptionalProfile.filter(id=optional.id).select_related(
            "owner"
        )
        assert cleared.owner_id is None and cleared.owner is None
    return linked.id
