"""Regressions found reviewing many-to-many: schema ids, declaration mistakes,
bridge adoption rules, scoped prefetch and keyword-only scopes."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki import Schema
from hinoki.db import Database
from hinoki.errors import FieldError, RegistrationError
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    field,
    foreign_key,
    many_to_many,
    migrator,
    use_database,
)
from hinoki.orm.migrations import plan
from hinoki.schema import Field


class Tag(Model, table="m2mr_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str


class Post(Model, table="m2mr_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, ids_name="tag_ids")


class PostOut(Schema):
    title: str
    tag_ids: set[UUID] = Field(default_factory=set)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    connection = await Database.connect(
        f"sqlite://{tmp_path / 'r.sqlite3'}", max_size=1, wait=0.5
    )
    plan([Tag, Post, *Post.__bridges__], tmp_path, "initial", dialect="sqlite")
    await migrator.apply(connection, tmp_path)
    with use_database(connection):
        yield connection
    connection.close()


async def test_from_model_reads_the_prefetched_ids_of_a_set_field(
    database: Database,
) -> None:
    tag = await Tag.acreate(name="a")
    post = await Post.acreate(title="p")
    await post.tags.aadd(tag)
    (row,) = await Post.all().prefetch_related("tags")
    assert PostOut.from_model(row).tag_ids == {tag.id}
    assert [o.tag_ids for o in PostOut.from_models([row])] == [{tag.id}]


async def test_prefetch_related_refuses_an_unknown_relation(
    database: Database,
) -> None:
    with pytest.raises(FieldError):
        Post.all().prefetch_related(nope=Tag.all())
    with pytest.raises(FieldError):
        Post.all().prefetch_related(tags="not a queryset")  # type: ignore[arg-type]


async def test_a_scoped_prefetch_skips_a_target_deleted_since_the_bridge_read(
    database: Database,
) -> None:
    keep = await Tag.acreate(name="keep")
    gone = await Tag.acreate(name="gone")
    post = await Post.acreate(title="p")
    await post.tags.aadd(keep, gone)
    await Tag.filter(id=gone.id).adelete()
    (row,) = await Post.all().prefetch_related(tags=Tag.filter(name="keep"))
    assert [t.id for t in row.tags.loaded] == [keep.id]


async def test_a_scope_is_keyword_only(database: Database) -> None:
    post = await Post.acreate(title="p")
    with pytest.raises(TypeError):
        post.tags.query(Tag.all())  # type: ignore[call-arg]
    assert await post.tags.acount(within=Tag.filter(name="x")) == 0
    assert await post.tags.aexists(within=Tag.filter(name="x")) is False


def test_a_relation_without_classvar_is_refused() -> None:
    with pytest.raises(RegistrationError, match="ClassVar"):

        class Bad(Model, table="m2mr_bad"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            tags: ManyToMany[Tag] = many_to_many(Tag)


def cascade(model: type[Model]) -> UUID:
    reference: UUID = foreign_key(model, on_delete=OnDelete.CASCADE)
    return reference


def owner_of(bridge: str, table: str) -> type[Model]:
    class Owner(Model, table=table):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, bridge=bridge)

    return Owner


def test_two_relations_cannot_share_one_bridge() -> None:
    owner_of("SharedBridge", "m2mr_shared_a")
    with pytest.raises(RegistrationError, match="already the bridge"):
        owner_of("SharedBridge", "m2mr_shared_b")


def test_the_foreign_keys_of_a_bridge_must_point_at_its_sides() -> None:
    owner = owner_of("WrongSide", "m2mr_wrong_owner")
    with pytest.raises(RegistrationError, match="foreign key to"):

        class WrongSide(Model, table="m2mr_wrong", unique=[("a_id", "tag_id")]):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            a_id: UUID = cascade(Tag)
            tag_id: UUID = cascade(Tag)

    assert owner.__links__ == {}


def test_a_nullable_link_column_is_refused() -> None:
    owner = owner_of("Nullable", "m2mr_nullable_owner")
    with pytest.raises(RegistrationError, match="must not be nullable"):

        class Nullable(Model, table="m2mr_nullable", unique=[("owner_id", "tag_id")]):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner_id: UUID | None = foreign_key(
                owner, on_delete=OnDelete.CASCADE, default=None
            )
            tag_id: UUID = cascade(Tag)


def test_a_bridge_with_a_write_hook_is_refused() -> None:
    owner = owner_of("Hooked", "m2mr_hooked_owner")
    with pytest.raises(RegistrationError, match="write hooks"):

        class Hooked(Model, table="m2mr_hooked", unique=[("owner_id", "tag_id")]):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner_id: UUID = cascade(owner)
            tag_id: UUID = cascade(Tag)

            async def before_save(self) -> None:
                return None


def owner_with_columns(bridge: str, table: str, columns: tuple[str, str]) -> None:
    class Owner(Model, table=table):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        tags: ClassVar[ManyToMany[Tag]] = many_to_many(
            Tag, bridge=bridge, columns=columns
        )


def test_the_key_cannot_be_a_link_column() -> None:
    owner_with_columns("KeyColumn", "m2mr_key_owner", ("id", "tag_id"))
    with pytest.raises(RegistrationError, match="cannot be a link column"):

        class KeyColumn(Model, table="m2mr_key", unique=[("id", "tag_id")]):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            tag_id: UUID = cascade(Tag)


def test_a_named_column_the_bridge_lacks_is_refused() -> None:
    owner_with_columns("Lacking", "m2mr_lacking_owner", ("owner_id", "tag_id"))
    with pytest.raises(RegistrationError, match="no column"):

        class Lacking(Model, table="m2mr_lacking"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            tag_id: UUID = cascade(Tag)
