"""Opt-in concurrent pagination on ``QuerySet.apaginate(..., concurrent=True)``."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from hinoki.db import Database, DatabaseError
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    QuerySet,
    field,
    foreign_key,
    many_to_many,
    use_database,
)
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.pagination import DEFAULT_PAGE_SIZE, Page, PageParams


class Author(Model, table="cp_authors"):
    id: int = field(primary_key=True)
    name: str


class Tag(Model, table="cp_tags"):
    id: int = field(primary_key=True)
    label: str


class Article(Model, table="cp_articles"):
    id: int = field(primary_key=True)
    title: str
    rank: int = 0
    published: bool = True
    author_id: int | None = foreign_key(
        Author, on_delete=OnDelete.CASCADE, related_name="articles"
    )
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(
        Tag, table="cp_article_tags", related_name="articles"
    )


MODELS: tuple[type[Model], ...] = (Author, Tag, Article, *Article.__bridges__)
BACKENDS = {
    "sqlite": None,
    "postgres": os.environ.get("HINOKI_TEST_DATABASE_URL"),
}


async def _setup_tables(database: Database) -> None:
    for model in reversed(MODELS):
        await database.execute(f'DROP TABLE IF EXISTS "{model.__table__}"')
    for model in MODELS:
        sql = create_table_sql(model.__table__, table_state(model), database.dialect)
        await database.execute(sql)


async def _seed_articles() -> None:
    alice = await Author.acreate(id=1, name="Alice")
    bob = await Author.acreate(id=2, name="Bob")
    t_rust = await Tag.acreate(id=1, label="rust")
    t_py = await Tag.acreate(id=2, label="python")
    for index in range(1, 8):
        author = alice if index % 2 == 1 else bob
        article = await Article.acreate(
            id=index,
            title=f"post-{index}",
            rank=index // 3,  # intentional ties for ordering stability
            published=index != 4,
            author_id=author.id,
        )
        if index in (1, 2, 3):
            await article.tags.aadd(t_rust)
        if index in (2, 5):
            await article.tags.aadd(t_py)


@pytest.fixture(
    params=[
        pytest.param(
            name,
            marks=pytest.mark.skipif(
                name != "sqlite" and url is None, reason=f"no {name} database URL"
            ),
        )
        for name, url in BACKENDS.items()
    ]
)
async def database(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Database]:
    backend = request.param
    url = BACKENDS[backend] or f"sqlite://{tmp_path / 'concurrent_page.sqlite3'}"
    db = await Database.connect(url, max_size=4, wait=2.0)
    await _setup_tables(db)
    try:
        with use_database(db):
            yield db
    finally:
        db.close()


def _rel(instance: Any, name: str) -> Any:
    return getattr(instance, name)


def _page_signature(page: Page[Article]) -> tuple[list[int], int, int, int, int]:
    return (
        [item.id for item in page.items],
        page.total,
        page.page,
        page.page_size,
        page.pages,
    )


async def test_empty_table_and_default_params(database: Database) -> None:
    seq = await Article.all().apaginate(PageParams(1, 3), concurrent=False)
    conc = await Article.all().apaginate(PageParams(1, 3), concurrent=True)
    assert _page_signature(conc) == _page_signature(seq) == ([], 0, 1, 3, 0)

    default_page = await Article.all().apaginate(concurrent=True)
    assert (default_page.page, default_page.page_size, default_page.total) == (
        1,
        DEFAULT_PAGE_SIZE,
        0,
    )


async def test_parity_normal_out_of_range_filtered_and_ordering_ties(
    database: Database,
) -> None:
    await _seed_articles()

    queries = [
        Article.all().order_by("rank"),
        Article.all().order_by("-rank"),
        Article.filter(published=True).order_by("rank"),
        Article.filter(title="missing"),
    ]
    pages = [PageParams(1, 3), PageParams(2, 3), PageParams(3, 3), PageParams(9, 3)]
    for query in queries:
        for params in pages:
            seq = await query.apaginate(params, concurrent=False)
            conc = await query.apaginate(params, concurrent=True)
            assert _page_signature(conc) == _page_signature(seq)


async def test_parity_select_related_and_prefetch_related(database: Database) -> None:
    await _seed_articles()
    params = PageParams(1, 4)

    seq_articles = (
        await Article.all()
        .select_related("author")
        .prefetch_related("tags")
        .order_by("id")
        .apaginate(params, concurrent=False)
    )
    conc_articles = (
        await Article.all()
        .select_related("author")
        .prefetch_related("tags")
        .order_by("id")
        .apaginate(params, concurrent=True)
    )
    assert _page_signature(conc_articles) == _page_signature(seq_articles)
    assert [_rel(a, "author").name for a in conc_articles.items] == [
        _rel(a, "author").name for a in seq_articles.items
    ]
    assert [sorted(t.label for t in a.tags.loaded) for a in conc_articles.items] == [
        sorted(t.label for t in a.tags.loaded) for a in seq_articles.items
    ]

    seq_authors = (
        await Author.all()
        .prefetch_related("articles")
        .order_by("id")
        .apaginate(PageParams(1, 2), concurrent=False)
    )
    conc_authors = (
        await Author.all()
        .prefetch_related("articles")
        .order_by("id")
        .apaginate(PageParams(1, 2), concurrent=True)
    )
    assert [
        (a.id, sorted(art.id for art in _rel(a, "articles")))
        for a in conc_authors.items
    ] == [
        (a.id, sorted(art.id for art in _rel(a, "articles"))) for a in seq_authors.items
    ]


async def test_custom_build_falls_back_to_fetch_page_rows(database: Database) -> None:
    await _seed_articles()
    built: list[int] = []

    class CustomQuerySet(QuerySet[Article]):
        def derive(self, **changes: Any) -> QuerySet[Article]:
            base = super().derive(**changes)
            derived = CustomQuerySet(
                base.model,
                base.filters,
                base.order,
                base.limit_to,
                base.offset_by,
                base.loading,
            )
            derived.implicit_filter_count = base.implicit_filter_count
            return derived

        def build(self, row: dict[str, Any]) -> Article:
            built.append(int(row["id"]))
            return super().build(row)

    page = (
        await CustomQuerySet(Article)
        .order_by("id")
        .apaginate(PageParams(1, 3), concurrent=True)
    )
    assert _page_signature(page) == ([1, 2, 3], 7, 1, 3, 3)
    assert built == [1, 2, 3]


async def test_pool_size_one_does_not_deadlock(tmp_path: Path) -> None:
    urls = [f"sqlite://{tmp_path / 'single.sqlite3'}"]
    if BACKENDS["postgres"]:
        urls.append(BACKENDS["postgres"])
    for url in urls:
        db = await Database.connect(url, max_size=1, wait=1.0)
        try:
            await _setup_tables(db)
            with use_database(db):
                await _seed_articles()
                page = await asyncio.wait_for(
                    Article.all()
                    .order_by("id")
                    .apaginate(PageParams(2, 3), concurrent=True),
                    timeout=2.0,
                )
                assert _page_signature(page) == ([4, 5, 6], 7, 2, 3, 3)
        finally:
            db.close()


async def test_transaction_runs_sequentially_and_sees_uncommitted_writes(
    database: Database,
) -> None:
    await _seed_articles()

    async with database.atomic():
        await Article.acreate(
            id=99, title="uncommitted", rank=0, published=True, author_id=None
        )
        page = (
            await Article.all()
            .order_by("id")
            .apaginate(PageParams(3, 3), concurrent=True)
        )
        assert page.total == 8
        assert [item.id for item in page.items] == [7, 99]
        # Force rollback to verify transaction semantics are preserved
        try:
            async with database.atomic():
                await Article.acreate(
                    id=100, title="sp", rank=0, published=True, author_id=None
                )
                sp_page = (
                    await Article.all()
                    .order_by("id")
                    .apaginate(PageParams(3, 3), concurrent=True)
                )
                assert sp_page.total == 9
                raise RuntimeError("rollback savepoint")
        except RuntimeError:
            pass
        after_sp = (
            await Article.all()
            .order_by("id")
            .apaginate(PageParams(3, 3), concurrent=True)
        )
        assert after_sp.total == 8

    committed = (
        await Article.all().order_by("id").apaginate(PageParams(3, 3), concurrent=True)
    )
    assert committed.total == 8


async def test_error_propagation_and_cancellation_do_not_leak_connections(
    database: Database,
) -> None:
    await _seed_articles()
    plan = Article.row_plan()
    assert plan is not None

    with pytest.raises(DatabaseError):
        await database.fetch_page_models(
            "SELECT * FROM nonexistent_table_xyz",
            [],
            'SELECT count(*) AS count FROM "cp_articles"',
            [],
            plan,
            concurrent=True,
        )

    with pytest.raises(DatabaseError):
        await database.fetch_page_rows(
            'SELECT * FROM "cp_articles" LIMIT 1',
            [],
            "SELECT count(*) FROM nonexistent_table_xyz",
            [],
            concurrent=True,
        )

    task = asyncio.create_task(
        Article.all().order_by("id").apaginate(PageParams(1, 3), concurrent=True)
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Pool remains healthy and connections are returned.
    healthy = (
        await Article.all().order_by("id").apaginate(PageParams(1, 3), concurrent=True)
    )
    assert _page_signature(healthy) == ([1, 2, 3], 7, 1, 3, 3)
    size, idle = database.status()
    assert idle == size
