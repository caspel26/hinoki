"""Models: SQL compilation (always) and persistence (needs a disposable PostgreSQL)."""

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import RegistrationError
from hinoki.db import Database, UniqueViolation
from hinoki.errors import FieldError, MultipleObjectsReturned, ObjectNotFound
from hinoki.orm import Model, QuerySet, field, use_database
from hinoki.pagination import PageParams

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


class Author(Model, table="orm_authors"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80, unique=True)


class Post(Model, table="orm_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    stars: int = 0
    published: bool = False
    note: str | None = None


def sql_of(query: QuerySet[Any]) -> tuple[str, list[Any]]:
    return query.compile()


def test_filters_order_and_paging_compile_to_parameterised_sql() -> None:
    query = (
        Post.filter(published=True, stars__gte=3, title__in=["a", "b"])
        .order_by("-stars", "title")
        .limit(5)
        .offset(10)
    )
    sql, params = sql_of(query)
    assert sql == (
        'SELECT "id", "title", "stars", "published", "note" FROM "orm_posts" '
        'WHERE "published" = $1 AND "stars" >= $2 AND "title" IN ($3, $4) '
        'ORDER BY "stars" DESC, "title" ASC LIMIT 5 OFFSET 10'
    )
    assert params == [True, 3, "a", "b"]


def test_none_and_isnull_become_null_checks() -> None:
    assert sql_of(Post.filter(note=None))[0].endswith('WHERE "note" IS NULL')
    assert sql_of(Post.filter(note__ne=None))[0].endswith('"note" IS NOT NULL')
    assert sql_of(Post.filter(note__isnull=False))[0].endswith('"note" IS NOT NULL')


def test_querysets_are_immutable() -> None:
    base = Post.filter(published=True)
    narrowed = base.filter(stars=1)
    assert len(base.filters) == 1
    assert len(narrowed.filters) == 2


@pytest.mark.parametrize(
    "build",
    [
        lambda: Post.filter(nope=1),
        lambda: Post.filter(title__gt__lt=1),
        lambda: Post.filter(title__gt=None),
        lambda: Post.all().order_by("-nope"),
        lambda: Post.filter(**{'title" OR "1"="1': 1}),
    ],
)
def test_unknown_fields_and_hostile_names_are_rejected(build: Any) -> None:
    with pytest.raises(FieldError):
        build()


def test_model_declaration_errors_fail_at_class_creation() -> None:
    with pytest.raises(RegistrationError, match="table"):
        type("NoTable", (Model,), {"__annotations__": {}})
    with pytest.raises(RegistrationError, match="primary_key"):
        type("NoKey", (Model,), {"__annotations__": {"x": int}}, table="t")


def test_each_model_has_its_own_not_found() -> None:
    assert issubclass(Post.NotFound, ObjectNotFound)
    assert Post.NotFound is not Author.NotFound
    assert Post.NotFound.__qualname__ == "Post.NotFound"


def test_instances_are_checked_dataclasses() -> None:
    post = Post(title="x")
    assert (post.stars, post.published, post.note) == (0, False, None)
    with pytest.raises(TypeError):
        Post()  # type: ignore[call-arg]


async def test_models_need_an_active_database() -> None:
    with pytest.raises(RuntimeError, match="no database is active"):
        await Post.all().aall()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=3, wait=0.5)
    for table in ("orm_posts", "orm_authors"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await database.execute(
        "CREATE TABLE orm_authors (id uuid PRIMARY KEY, name text UNIQUE NOT NULL)"
    )
    await database.execute(
        "CREATE TABLE orm_posts (id uuid PRIMARY KEY, title text NOT NULL, "
        "stars int NOT NULL, published bool NOT NULL, note text)"
    )
    with use_database(database):
        yield database
    for table in ("orm_posts", "orm_authors"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


@needs_database
async def test_create_get_update_delete_round_trip(db: Database) -> None:
    post = await Post.acreate(title="Hello", stars=2)
    assert isinstance(post, Post)
    assert (await Post.aget(id=post.id)).title == "Hello"
    updated = await Post.aupdate(id=post.id, title="Hi", published=True)
    assert (updated.title, updated.published, updated.stars) == ("Hi", True, 2)
    assert await Post.adelete(id=post.id) == 1
    assert await Post.aget_or_none(id=post.id) is None


@needs_database
async def test_missing_and_ambiguous_rows(db: Database) -> None:
    with pytest.raises(Post.NotFound):
        await Post.aget(id=uuid4())
    with pytest.raises(Post.NotFound):
        await Post.aupdate(id=uuid4(), title="x")
    await Post.acreate(title="same")
    await Post.acreate(title="same")
    with pytest.raises(MultipleObjectsReturned):
        await Post.aget(title="same")


@needs_database
async def test_queries_filter_order_count_and_paginate(db: Database) -> None:
    for n in range(5):
        await Post.acreate(title=f"p{n}", stars=n, published=n % 2 == 0)
    published = Post.filter(published=True).order_by("-stars")
    assert [p.title for p in await published.aall()] == ["p4", "p2", "p0"]
    assert await published.acount() == 3
    assert (await Post.filter(stars__in=[1, 3]).acount()) == 2
    assert (await Post.filter(stars__in=[]).acount()) == 0
    assert (await published.afirst()) is not None
    page = await published.apaginate(PageParams(page=2, page_size=2))
    assert ([p.title for p in page.items], page.total, page.pages) == (["p0"], 3, 2)
    assert (await Post.all().apaginate(PageParams(1, 2))).total == 5


@needs_database
async def test_null_filters_find_null_columns(db: Database) -> None:
    await Post.acreate(title="plain")
    await Post.acreate(title="noted", note="n")
    assert (await Post.filter(note=None).acount()) == 1
    assert (await Post.filter(note__ne=None).acount()) == 1


@needs_database
async def test_a_hostile_value_is_data_not_sql(db: Database) -> None:
    await Post.acreate(title="x'; DROP TABLE orm_posts; --")
    assert await Post.filter(title="x'; DROP TABLE orm_posts; --").acount() == 1
    assert await Post.all().acount() == 1


@needs_database
async def test_adelete_refuses_to_wipe_a_table(db: Database) -> None:
    await Post.acreate(title="keep")
    with pytest.raises(FieldError):
        await Post.adelete()
    assert await Post.all().acount() == 1


@needs_database
async def test_constraint_errors_surface_as_typed_database_errors(db: Database) -> None:
    await Author.acreate(name="ada")
    with pytest.raises(UniqueViolation):
        await Author.acreate(name="ada")


@needs_database
async def test_models_follow_the_transaction_scope(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await Post.acreate(title="rolled back")
            raise RuntimeError
    assert await Post.all().acount() == 0
    async with db.transaction():
        await Post.acreate(title="kept")
    assert await Post.all().acount() == 1
