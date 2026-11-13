"""Many-to-many as an explicit join model with composite uniqueness."""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki import RegistrationError
from hinoki.db import Database, UniqueViolation
from hinoki.errors import MigrationError
from hinoki.orm import Model, field, foreign_key, migrator, use_database
from hinoki.orm.migrations import plan

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


class Post(Model, table="m2m_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str


class Tag(Model, table="m2m_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    label: str


class PostTag(Model, table="m2m_post_tags", unique=[("post_id", "tag_id")]):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    post_id: UUID = foreign_key(Post, on_delete="cascade", related_name="links")
    tag_id: UUID = foreign_key(Tag, on_delete="cascade", related_name="links")


def test_composite_unique_is_recorded_on_the_model() -> None:
    assert PostTag.__unique__ == (("post_id", "tag_id"),)
    assert Post.__unique__ == ()


@pytest.mark.parametrize(
    "groups", [[("title",)], [("title", "title")], [("title", "nope")]]
)
def test_invalid_composite_unique_is_refused(groups: list[tuple[str, ...]]) -> None:
    with pytest.raises(RegistrationError, match="unique="):
        type(
            "Bad",
            (Model,),
            {
                "__annotations__": {"id": UUID, "title": str},
                "id": field(primary_key=True),
            },
            table="m2m_bad",
            unique=groups,
        )


def test_the_plan_creates_the_constraint_and_refuses_to_change_it(
    tmp_path: Path,
) -> None:
    result = plan([Post, Tag, PostTag], tmp_path, "initial")
    assert result.path is not None
    assert 'UNIQUE ("post_id", "tag_id")' in result.path.read_text()
    assert not plan([Post, Tag, PostTag], tmp_path, "again").operations
    loose = type(
        "PostTag",
        (Model,),
        {
            "__annotations__": {"id": UUID, "post_id": UUID, "tag_id": UUID},
            "id": field(primary_key=True),
            "post_id": foreign_key(Post, on_delete="cascade"),
            "tag_id": foreign_key(Tag, on_delete="cascade"),
        },
        table="m2m_post_tags",
    )
    with pytest.raises(MigrationError, match="composite unique"):
        plan([Post, Tag, loose], tmp_path, "change")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2, wait=0.5)
    tables = ("m2m_post_tags", "m2m_tags", "m2m_posts", "hinoki_migrations")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    plan([Post, Tag, PostTag], tmp_path, "initial")
    await migrator.apply(database, tmp_path)
    with use_database(database):
        yield database
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


@needs_database
async def test_tags_load_through_the_join_model_and_pairs_are_unique(
    db: Database,
) -> None:
    post = await Post.acreate(title="p")
    red, blue = await Tag.acreate(label="red"), await Tag.acreate(label="blue")
    for tag in (red, blue):
        await PostTag.acreate(post_id=post.id, tag_id=tag.id)
    with pytest.raises(UniqueViolation):
        await PostTag.acreate(post_id=post.id, tag_id=red.id)
    loaded = await Post.all().prefetch_related("links").aget()
    assert len(loaded.links) == 2  # type: ignore[attr-defined]
    links = await PostTag.filter(post_id=loaded.id).select_related("tag").aall()
    assert {link.tag.label for link in links} == {"red", "blue"}  # type: ignore[attr-defined]
    tagged = await PostTag.filter(tag__label="red").select_related("post").aall()
    assert [link.post.title for link in tagged] == ["p"]  # type: ignore[attr-defined]
