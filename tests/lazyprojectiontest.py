"""Guarded Rust ``Row -> JSON`` projection and lazy ``_ProjectedList`` /
``_ProjectedPage``."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import gc
import weakref
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, ClassVar, cast

import pytest

from hinoki import (
    Field,
    Hinoki,
    RouteGroup,
    Schema,
    computed_field,
    core,
)
from hinoki.cli.routes import lanes_of
from hinoki.db import Database, DatabaseModule
from hinoki.errors import FieldError, MultipleObjectsReturned
from hinoki.orm import (
    ManyToMany,
    Model,
    OnDelete,
    Related,
    field,
    foreign_key,
    many_to_many,
    use_database,
)
from hinoki.orm import queryset as qs_mod
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.orm.projection import (
    _PLAN_CACHE,
    _ProjectedItem,
    _ProjectedList,
    _ProjectedPage,
    compile_projection_plan,
)
from hinoki.orm.queryset import QuerySet
from hinoki.pagination import Page, PageNumberPagination, PageParams
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import project_response
from hinoki.runtime.responsecheck import compile_response_check
from hinoki.schema import object_check
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch


class ProjAuthor(Model, table="lp_authors"):
    id: int = field(primary_key=True)
    name: str
    bio: str | None = None


class ProjTag(Model, table="lp_tags"):
    id: int = field(primary_key=True)
    label: str


class ProjArticle(Model, table="lp_articles"):
    id: int = field(primary_key=True)
    title: str
    views: int = 0
    published: bool = True
    summary: str | None = None
    created_at: datetime.datetime
    author_id: int | None = foreign_key(
        ProjAuthor, on_delete=OnDelete.CASCADE, related_name="articles"
    )
    author: ClassVar[Related[ProjAuthor | None]]
    tags: ClassVar[ManyToMany[ProjTag]] = many_to_many(
        ProjTag, table="lp_article_tags", related_name="articles"
    )


class AuthorOut(Schema):
    id: int
    name: str
    bio: str | None = None


class ArticleOut(Schema):
    id: int
    title: str = Field(alias="headline")
    views: int
    published: bool
    summary: str | None
    created_at: datetime.datetime


class ArticleWithAuthorOut(Schema):
    id: int
    title: str = Field(alias="headline")
    author: AuthorOut | None


class OtherOut(Schema):
    id: int
    code: str


class TagOut(Schema):
    id: int
    label: str


class ArticleWithTagsOut(Schema):
    id: int
    title: str = Field(alias="headline")
    author: AuthorOut | None
    tags: list[TagOut]


class AuthorWithArticlesOut(Schema):
    id: int
    name: str
    articles: list[ArticleOut]


class ComputedArticleOut(Schema):
    id: int
    title: str

    @computed_field
    def label(self) -> str:
        return f"#{self.id}:{self.title}"


class CheckedArticleOut(Schema):
    id: int
    title: str

    @object_check
    def non_empty(self) -> None:
        assert self.title


class CustomDumpArticleOut(Schema):
    id: int
    title: str

    def dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": self.id, "title": self.title.upper()}


MODELS: tuple[type[Model], ...] = (
    ProjAuthor,
    ProjTag,
    ProjArticle,
    *ProjArticle.__bridges__,
)


async def _setup_db(database: Database) -> None:
    for model in reversed(MODELS):
        await database.execute(f'DROP TABLE IF EXISTS "{model.__table__}"')
    for model in MODELS:
        sql = create_table_sql(model.__table__, table_state(model), database.dialect)
        await database.execute(sql)


async def _seed() -> None:
    alice = await ProjAuthor.acreate(id=1, name="Alice", bio="Systems")
    bob = await ProjAuthor.acreate(id=2, name="Bob", bio=None)
    tag = await ProjTag.acreate(id=1, label="rust")
    base = datetime.datetime(2026, 4, 1, 12, 30, 0, tzinfo=datetime.UTC)
    rows = [
        (1, "First", 10, True, "intro", base, alice.id),
        (2, "Second", 25, False, None, base + datetime.timedelta(hours=1), bob.id),
        (3, "Orphan", 5, True, "no author", base + datetime.timedelta(hours=2), None),
        (
            4,
            "Fourth",
            40,
            True,
            "deep dive",
            base + datetime.timedelta(hours=3),
            alice.id,
        ),
    ]
    for row_id, title, views, published, summary, created_at, author_id in rows:
        article = await ProjArticle.acreate(
            id=row_id,
            title=title,
            views=views,
            published=published,
            summary=summary,
            created_at=created_at,
            author_id=author_id,
        )
        if row_id in (1, 4):
            await article.tags.aadd(tag)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    url = f"sqlite:///{tmp_path / 'lazy_projection.sqlite3'}"
    database = await Database.connect(url)
    try:
        await _setup_db(database)
        with use_database(database):
            await _seed()
            yield database
    finally:
        database.close()


async def test_flat_list_byte_for_byte_parity(db: Database) -> None:
    query = ProjArticle.all().order_by("id")
    expected_models = await query
    expected_schemas = [ArticleOut.from_model(m) for m in expected_models]
    expected_bytes = core.encode_json(expected_schemas)

    projected = await query.aproject(ArticleOut)
    assert isinstance(projected, _ProjectedList)
    assert projected._projected_handle is not None
    assert len(projected) == 4
    assert bool(projected) is True
    # len/bool must not trigger materialization
    assert projected._projected_handle is not None

    actual_bytes = core.encode_json(projected)
    assert actual_bytes == expected_bytes
    assert projected._projected_handle is not None

    # Non-compact modes must also match byte-for-byte
    assert core.encode_json(projected, indent=2) == core.encode_json(
        expected_schemas, indent=2
    )
    assert core.encode_json(projected, sort_keys=True) == core.encode_json(
        expected_schemas, sort_keys=True
    )


async def test_page_byte_for_byte_parity(db: Database) -> None:
    for concurrent in (False, True):
        for params in (PageParams(1, 2), PageParams(2, 2), PageParams(5, 2)):
            query = ProjArticle.all().order_by("id")
            expected = (await query.apaginate(params, concurrent=concurrent)).map(
                ArticleOut.from_model
            )
            projected = await query.apaginate_as(
                ArticleOut, params, concurrent=concurrent
            )
            assert isinstance(projected, _ProjectedPage)
            assert projected._projected_handle is not None
            assert projected.page == expected.page
            assert projected.page_size == expected.page_size
            assert projected.total == expected.total
            assert projected.pages == expected.pages
            assert core.encode_json(projected) == core.encode_json(expected)
            assert projected._projected_handle is not None


async def test_select_related_nested_parity(db: Database) -> None:
    query = ProjArticle.all().select_related("author").order_by("id")
    expected_list = [ArticleWithAuthorOut.from_model(m) for m in await query]
    projected_list = await query.aproject(ArticleWithAuthorOut)
    assert isinstance(projected_list, _ProjectedList)
    assert projected_list._projected_handle is not None
    assert core.encode_json(projected_list) == core.encode_json(expected_list)

    params = PageParams(1, 3)
    expected_page = (await query.apaginate(params)).map(ArticleWithAuthorOut.from_model)
    projected_page = await query.apaginate_as(ArticleWithAuthorOut, params)
    assert isinstance(projected_page, _ProjectedPage)
    assert projected_page._projected_handle is not None
    assert core.encode_json(projected_page) == core.encode_json(expected_page)
    # Materializing nested objects matches Python models (including None author)
    assert projected_page.items == expected_page.items
    assert projected_page.items[2].author is None


async def test_lazy_materialization_does_not_requery_and_tracks_mutations(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    orig_list = db.fetch_projected_list
    orig_page = db.fetch_projected_page
    orig_fetch = db.fetch

    async def count_list(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await orig_list(*args, **kwargs)

    async def count_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await orig_page(*args, **kwargs)

    async def count_fetch(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await orig_fetch(*args, **kwargs)

    monkeypatch.setattr(db, "fetch_projected_list", count_list)
    monkeypatch.setattr(db, "fetch_projected_page", count_page)
    monkeypatch.setattr(db, "fetch", count_fetch)

    projected = await ProjArticle.all().order_by("id").aproject(ArticleOut)
    assert calls == 1
    assert isinstance(projected, _ProjectedList)
    assert projected._projected_handle is not None

    first = projected[0]
    assert isinstance(first, ArticleOut)
    assert first.title == "First"
    assert projected._projected_handle is None
    assert calls == 1

    # Mutation after materialization must be reflected in encode_json
    extra = ArticleOut(
        id=99,
        title="Appended",
        views=1,
        published=True,
        summary=None,
        created_at=first.created_at,
    )
    projected.append(extra)
    assert len(projected) == 5
    assert b"Appended" in core.encode_json(projected)
    assert calls == 1

    # Same for _ProjectedPage
    page = (
        await ProjArticle.all()
        .order_by("id")
        .apaginate_as(ArticleOut, PageParams(1, 2))
    )
    assert calls == 2
    assert isinstance(page, _ProjectedPage)
    assert page._projected_handle is not None
    items = page.items
    assert len(items) == 2
    assert items[0].title == "First"
    assert page._projected_handle is None
    assert calls == 2


async def test_fallbacks_preserve_exact_parity(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = ProjArticle.all().order_by("id")
    models = await query

    for schema in (ComputedArticleOut, CheckedArticleOut, CustomDumpArticleOut):
        expected = [schema.from_model(m) for m in models]
        projected = await query.aproject(schema)
        assert type(projected) is list
        assert core.encode_json(projected) == core.encode_json(expected)

        expected_page = (await query.apaginate(PageParams(1, 2))).map(schema.from_model)
        projected_page = await query.apaginate_as(schema, PageParams(1, 2))
        assert type(projected_page) is Page
        assert core.encode_json(projected_page) == core.encode_json(expected_page)

    # Custom model from_row() falls back cleanly
    orig_from_row = ProjArticle.from_row
    built_count = 0

    @classmethod  # type: ignore[misc]
    def custom_from_row(cls: type[ProjArticle], row: dict[str, Any]) -> ProjArticle:
        nonlocal built_count
        built_count += 1
        return orig_from_row(row)

    monkeypatch.setattr(ProjArticle, "from_row", custom_from_row)
    projected_custom = await query.aproject(ArticleOut)
    assert type(projected_custom) is list
    assert built_count == 4
    assert core.encode_json(projected_custom) == core.encode_json(
        [ArticleOut.from_model(m) for m in models]
    )


async def test_validate_response_and_atomic_http_endpoints(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'http_projection.sqlite3'}"
    database = await Database.connect(url)
    await _setup_db(database)
    with use_database(database):
        await _seed()
    database.close()

    group = RouteGroup("")
    observed_handles: dict[str, bool] = {}

    @group.get("/articles", validate_response=True)
    async def list_articles() -> list[ArticleOut]:
        result = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        observed_handles["list"] = (
            isinstance(result, _ProjectedList) and result._projected_handle is not None
        )
        return result

    @group.get(
        "/articles/page",
        pagination=PageNumberPagination(page_size=2),
        validate_response=True,
    )
    async def page_articles(page: PageParams) -> Page[ArticleOut]:
        result = await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, page)
        observed_handles["page"] = (
            isinstance(result, _ProjectedPage) and result._projected_handle is not None
        )
        return result

    @group.get("/articles/wrong-list", validate_response=True)
    async def wrong_list() -> list[OtherOut]:
        return await ProjArticle.all().order_by("id").aproject(ArticleOut)  # type: ignore[arg-type]

    @group.get(
        "/articles/wrong-page",
        pagination=PageNumberPagination(page_size=2),
        validate_response=True,
    )
    async def wrong_page(page: PageParams) -> Page[OtherOut]:
        return await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, page)  # type: ignore[arg-type]

    @group.post("/articles/atomic-ok", atomic=True, validate_response=True)
    async def atomic_ok() -> list[ArticleOut]:
        await ProjArticle.acreate(
            id=10,
            title="AtomicCreated",
            views=1,
            published=True,
            summary=None,
            created_at=datetime.datetime(2026, 4, 2, 0, 0, 0, tzinfo=datetime.UTC),
            author_id=1,
        )
        return await ProjArticle.all().order_by("id").aproject(ArticleOut)

    @group.post("/articles/atomic-rollback", atomic=True, validate_response=True)
    async def atomic_rollback() -> list[OtherOut]:
        await ProjArticle.acreate(
            id=11,
            title="ShouldRollback",
            views=1,
            published=True,
            summary=None,
            created_at=datetime.datetime(2026, 4, 2, 0, 0, 0, tzinfo=datetime.UTC),
            author_id=1,
        )
        # Response validation mismatch must roll back the transaction before commit
        return await ProjArticle.all().order_by("id").aproject(ArticleOut)  # type: ignore[arg-type]

    @group.get("/articles/sync", validate_response=True)
    def sync_articles() -> list[ArticleOut]:
        return ProjArticle.all().order_by("id").project(ArticleOut)

    @group.get(
        "/articles/sync-page",
        pagination=PageNumberPagination(page_size=2),
        validate_response=True,
    )
    def sync_page(page: PageParams) -> Page[ArticleOut]:
        return ProjArticle.all().order_by("id").paginate_as(ArticleOut, page)

    app = Hinoki(modules=[DatabaseModule(url)], include=[group])

    async with ASGIClient(app) as client:
        r_list = await client.get("/articles")
        assert r_list.status == 200
        assert observed_handles["list"] is True
        body = r_list.body
        assert len(body) == 4
        assert body[0]["headline"] == "First"

        r_page = await client.get("/articles/page", query="page=1&page_size=2")
        assert r_page.status == 200
        assert observed_handles["page"] is True
        assert r_page.body["total"] == 4
        assert len(r_page.body["items"]) == 2

        # Mismatched schema is strictly rejected with 500
        assert (await client.get("/articles/wrong-list")).status == 500
        assert (
            await client.get("/articles/wrong-page", query="page=1&page_size=2")
        ).status == 500

        # atomic=True commits on success and rolls back on contract error
        r_ok = await client.post("/articles/atomic-ok")
        assert r_ok.status == 200
        assert len(r_ok.body) == 5

        r_rb = await client.post("/articles/atomic-rollback")
        assert r_rb.status == 500
        # Row 11 must not have been committed
        assert len((await client.get("/articles")).body) == 5

        # Sync twins match async endpoints
        assert (await client.get("/articles/sync")).raw == (
            await client.get("/articles")
        ).raw
        assert (
            await client.get("/articles/sync-page", query="page=1&page_size=2")
        ).raw == (await client.get("/articles/page", query="page=1&page_size=2")).raw


async def test_native_lane_emits_identical_bytes(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'native_projection.sqlite3'}"
    database = await Database.connect(url)
    await _setup_db(database)
    with use_database(database):
        await _seed()
    database.close()

    def build_app() -> Hinoki:
        group = RouteGroup("")

        @group.get("/list", validate_response=True)
        async def list_projected() -> list[ArticleOut]:
            return await ProjArticle.all().order_by("id").aproject(ArticleOut)

        @group.get("/page", validate_response=True)
        async def page_projected(page: int = 1, page_size: int = 2) -> Page[ArticleOut]:
            return (
                await ProjArticle.all()
                .order_by("id")
                .apaginate_as(ArticleOut, PageParams(page, page_size))
            )

        @group.get("/joined", validate_response=True)
        async def joined_projected() -> list[ArticleWithAuthorOut]:
            return (
                await ProjArticle.all()
                .select_related("author")
                .order_by("id")
                .aproject(ArticleWithAuthorOut)
            )

        return Hinoki(modules=[DatabaseModule(url)], include=[group])

    async def exercise(
        *, native_dispatch: bool
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        app = build_app()
        worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native_dispatch)
        port = await worker.start()
        try:
            lanes = {r.path: lanes_of(app)[id(r)] for r in app.routes}
            out: dict[str, bytes] = {}
            for path in ("/list", "/page?page=1&page_size=2", "/joined"):
                resp = await fetch(port, "GET", path)
                assert resp.status == 200
                out[path] = resp.body
            return out, lanes
        finally:
            await worker.stop()

    native_bodies, native_lanes = await exercise(native_dispatch=True)
    py_bodies, _ = await exercise(native_dispatch=False)

    assert native_lanes["/list"][0] == "native"
    assert native_lanes["/page"][0] == "native"
    assert native_lanes["/joined"][0] == "native"
    assert native_bodies == py_bodies


async def test_unmaterialized_schema_validate_dump_copy_ordering_and_union_checks(
    db: Database,
) -> None:
    class AnyEnvelope(Schema):
        items: Any

    class TypedEnvelope(Schema):
        items: Annotated[list[Any], Field(min_length=1)]

    with use_database(db):
        # 1. Schema.validate / dump / to_dict on unmaterialized _ProjectedList
        proj_for_dump = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        assert isinstance(proj_for_dump, _ProjectedList)
        assert proj_for_dump._projected_handle is not None
        assert len(AnyEnvelope(items=proj_for_dump).dump()["items"]) == 4

        proj_for_dict = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        assert len(AnyEnvelope(items=proj_for_dict).to_dict()["items"]) == 4

        proj_for_validate = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        validated = TypedEnvelope.validate({"items": proj_for_validate})
        assert len(validated.items) == 4
        assert validated.items[0].title == "First"

        # 2. _ProjectedList ordering and copy / deepcopy
        proj_for_cmp = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        assert proj_for_cmp > []
        assert proj_for_cmp >= []

        proj_for_copy = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        shallow = copy.copy(proj_for_copy)
        assert len(shallow) == 4
        assert type(shallow) is list

        proj_for_deep = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        deep = copy.deepcopy(proj_for_deep)
        assert len(deep) == 4
        assert deep == shallow

        # 3. _ProjectedPage equality with Page and copy / deepcopy
        params = PageParams(page=1, page_size=2)
        proj_page = (
            await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, params)
        )
        normal_page = (await ProjArticle.all().order_by("id").apaginate(params)).map(
            ArticleOut.from_model
        )
        assert proj_page == normal_page
        assert normal_page == proj_page

        proj_page2 = (
            await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, params)
        )
        assert copy.copy(proj_page2) == normal_page
        proj_page3 = (
            await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, params)
        )
        assert copy.deepcopy(proj_page3) == normal_page

        # 4. compile_response_check with union / optional / Any annotations
        proj_list = await ProjArticle.all().order_by("id").aproject(ArticleOut)
        proj_page4 = (
            await ProjArticle.all().order_by("id").apaginate_as(ArticleOut, params)
        )
        for ann in (list[ArticleOut | OtherOut], list[ArticleOut | None], list[Any]):
            assert compile_response_check("act", ann)(proj_list) is True
        assert compile_response_check("act", list[OtherOut])(proj_list) is False

        for page_ann in (
            Page[ArticleOut | OtherOut],
            Page[ArticleOut | None],
            Page[Any],
        ):
            assert compile_response_check("act", page_ann)(proj_page4) is True
        assert compile_response_check("act", Page[OtherOut])(proj_page4) is False


def test_compile_projection_plan_cache_and_invalidation() -> None:
    q_flat = ProjArticle.all().order_by("id")
    plan1 = compile_projection_plan(q_flat, ArticleOut, "sqlite")
    plan2 = compile_projection_plan(q_flat, ArticleOut, "sqlite")
    assert plan1 is not None
    assert plan1 is plan2

    # Different dialect or loading produces a distinct cached plan
    plan_pg = compile_projection_plan(q_flat, ArticleOut, "postgres")
    assert plan_pg is not None
    assert plan_pg is not plan1

    q_joined = ProjArticle.all().select_related("author")
    plan_joined = compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite")
    assert plan_joined is not None
    assert (
        compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is plan_joined
    )

    # Cache uses WeakKeyDictionary on schema and invalidates on __computed_specs__
    assert isinstance(_PLAN_CACHE, weakref.WeakKeyDictionary)

    class TempOut(Schema):
        id: int
        title: str

    temp_plan = compile_projection_plan(q_flat, TempOut, "sqlite")
    assert temp_plan is not None
    assert TempOut in _PLAN_CACHE

    TempOut.__computed_specs__ = {"extra": cast(Any, object())}
    assert compile_projection_plan(q_flat, TempOut, "sqlite") is None


async def test_prefetch_m2m_and_reverse_fk_projection_and_lazy_materialization(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M2M + select_related list and paginated projection
    q_m2m = (
        ProjArticle.all()
        .select_related("author")
        .prefetch_related("tags")
        .order_by("id")
    )
    expected_models = await q_m2m
    expected_schemas = [ArticleWithTagsOut.from_model(m) for m in expected_models]
    expected_bytes = core.encode_json(expected_schemas)

    projected = await q_m2m.aproject(ArticleWithTagsOut)
    assert isinstance(projected, _ProjectedList)
    assert projected._projected_handle is not None
    assert core.encode_json(projected) == expected_bytes
    assert projected._projected_handle is not None

    # Paginated concurrent=True with prefetch_related matches byte-for-byte
    params = PageParams(page=1, page_size=2)
    expected_page = (await q_m2m.apaginate(params, concurrent=True)).map(
        ArticleWithTagsOut.from_model
    )
    projected_page = await q_m2m.apaginate_as(
        ArticleWithTagsOut, params, concurrent=True
    )
    assert isinstance(projected_page, _ProjectedPage)
    assert projected_page._projected_handle is not None
    assert core.encode_json(projected_page) == core.encode_json(expected_page)

    # Reverse FK prefetch projection
    q_rev = ProjAuthor.all().prefetch_related("articles").order_by("id")
    expected_authors = [AuthorWithArticlesOut.from_model(m) for m in await q_rev]
    projected_authors = await q_rev.aproject(AuthorWithArticlesOut)
    assert isinstance(projected_authors, _ProjectedList)
    assert projected_authors._projected_handle is not None
    assert core.encode_json(projected_authors) == core.encode_json(expected_authors)

    # Materialization must NOT issue any database queries
    async def forbid_fetch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Lazy materialization must not hit the database")

    monkeypatch.setattr(db, "fetch", forbid_fetch)
    assert list(projected) == expected_schemas
    assert list(projected_authors) == expected_authors

    # Scoped prefetch falls back cleanly to plain list
    monkeypatch.undo()
    q_scoped = (
        ProjArticle.all()
        .select_related("author")
        .prefetch_related(tags=ProjTag.filter(label="rust"))
        .order_by("id")
    )
    scoped_res = await q_scoped.aproject(ArticleWithTagsOut)
    assert type(scoped_res) is list
    assert core.encode_json(scoped_res) == expected_bytes


async def test_single_record_projection_get_as_and_get_or_none_as(
    db: Database,
) -> None:
    # Model & QuerySet aget_as with select_related + prefetch_related
    q_one = ProjArticle.filter(id=1).select_related("author").prefetch_related("tags")
    expected = ArticleWithTagsOut.from_model(await q_one.aget())
    expected_bytes = core.encode_json(expected)

    item = await q_one.aget_as(ArticleWithTagsOut)
    assert isinstance(item, _ProjectedItem)
    assert item._projected_handle is not None
    assert core.encode_json(item) == expected_bytes
    assert item._projected_handle is not None

    # validate_response checks O(1) without materializing
    assert compile_response_check("act", ArticleWithTagsOut)(item) is True
    assert compile_response_check("act", ArticleWithTagsOut | None)(item) is True
    assert compile_response_check("act", OtherOut)(item) is False
    assert item._projected_handle is not None

    # Non-compact JSON and attribute access materialize lazily with zero DB queries
    assert core.encode_json(item, indent=2) == core.encode_json(expected, indent=2)
    assert item == expected
    assert item.title == "First"
    assert len(item.tags) == 1
    assert item.tags[0].label == "rust"

    # 0 rows and >1 rows semantics
    with pytest.raises(ProjArticle.NotFound):
        await ProjArticle.aget_as(ArticleOut, id=999)
    assert await ProjArticle.aget_or_none_as(ArticleOut, id=999) is None
    with pytest.raises(MultipleObjectsReturned):
        await ProjArticle.all().aget_as(ArticleOut)
    with pytest.raises(MultipleObjectsReturned):
        await ProjArticle.all().aget_or_none_as(ArticleOut)


async def test_single_record_projection_schema_contract_and_native_lane(
    db: Database, tmp_path: Path
) -> None:
    fresh = await ProjArticle.aget_as(ArticleOut, id=1)
    expected_flat = ArticleOut.from_model(await ProjArticle.aget(id=1))
    assert isinstance(fresh, _ProjectedItem)
    assert isinstance(fresh, ArticleOut)
    assert isinstance(fresh, Schema)
    assert cast(Any, fresh)._projected_handle is not None

    # Embedding in another Schema, to_dict(), and hash() match a materialized Schema
    class WrapArticle(Schema):
        article: ArticleOut

    class AnyWrap(Schema):
        item: Any

    wrapped = WrapArticle(article=fresh)
    assert wrapped.article == expected_flat
    assert AnyWrap(item=fresh).to_dict() == AnyWrap(item=expected_flat).to_dict()
    assert hash(fresh) == hash(expected_flat)

    fresh2 = await ProjArticle.aget_as(ArticleOut, id=1)
    assert isinstance(fresh2, _ProjectedItem)
    assert fresh2._projected_handle is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        fresh2.title = "Mutated"  # type: ignore[misc]
    assert fresh2._projected_handle is None

    # Native lane accepts aget_as/aget_or_none_as; sync twins work in sync routes
    q_one = ProjArticle.filter(id=1).select_related("author").prefetch_related("tags")
    expected_bytes = core.encode_json(ArticleWithTagsOut.from_model(await q_one.aget()))
    group = RouteGroup("")

    @group.get("/item/{article_id}", validate_response=True)
    async def get_item(article_id: int) -> ArticleWithTagsOut:
        return (
            await ProjArticle.filter(id=article_id)
            .select_related("author")
            .prefetch_related("tags")
            .aget_as(ArticleWithTagsOut)
        )

    @group.get("/maybe/{article_id}", validate_response=True)
    async def maybe_item(article_id: int) -> ArticleOut | None:
        return await ProjArticle.aget_or_none_as(ArticleOut, id=article_id)

    @group.get("/sync-item/{article_id}", validate_response=True)
    def sync_get_item(article_id: int) -> ArticleOut:
        return ProjArticle.get_as(ArticleOut, id=article_id)

    @group.get("/sync-maybe/{article_id}", validate_response=True)
    def sync_maybe_item(article_id: int) -> ArticleOut | None:
        return ProjArticle.get_or_none_as(ArticleOut, id=article_id)

    url = f"sqlite:///{tmp_path / 'lazy_projection.sqlite3'}"
    app = Hinoki(modules=[DatabaseModule(url)], include=[group])
    lanes = {r.path: lanes_of(app)[id(r)] for r in app.routes}
    assert lanes["/item/{article_id}"][0] == "native"
    assert lanes["/maybe/{article_id}"][0] == "native"

    async with ASGIClient(app) as client:
        r_async = await client.get("/item/1")
        assert r_async.status == 200
        assert r_async.raw == expected_bytes

        r_sync = await client.get("/sync-item/1")
        assert r_sync.status == 200
        assert r_sync.raw == core.encode_json(
            await ProjArticle.aget_as(ArticleOut, id=1)
        )

        r_none = await client.get("/sync-maybe/999")
        assert r_none.status == 204
        assert r_none.body is None


def test_projection_plan_cache_invalidates_on_hooks_and_avoids_model_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    q_joined = ProjArticle.all().select_related("author")
    plan = compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite")
    assert plan is not None

    # Monkeypatching related model from_row invalidates the cached plan
    @classmethod  # type: ignore[misc]
    def custom_from_row(cls: type[ProjAuthor], row: dict[str, Any]) -> ProjAuthor:
        return cast(ProjAuthor, Model.from_row.__func__(cls, row))  # type: ignore[attr-defined]

    monkeypatch.setattr(ProjAuthor, "from_row", custom_from_row)
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is None
    monkeypatch.undo()
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is not None

    # Monkeypatching schema.dump or __checks__ invalidates the cached plan
    monkeypatch.setattr(ArticleWithAuthorOut, "dump", lambda _self: {})
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is None
    monkeypatch.undo()
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is not None

    monkeypatch.setattr(ArticleWithAuthorOut, "__checks__", (lambda v: v,))
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is None
    monkeypatch.undo()
    assert compile_projection_plan(q_joined, ArticleWithAuthorOut, "sqlite") is not None

    # Nested WeakKeyDictionary does not strongly retain dynamic Model classes
    class LongLivedOut(Schema):
        id: int
        title: str

    def _compile_dynamic() -> weakref.ref[type[Model]]:
        class DynamicArticle(Model):
            id: int = field(primary_key=True)
            title: str

        assert (
            compile_projection_plan(DynamicArticle.all(), LongLivedOut, "sqlite")
            is not None
        )
        return weakref.ref(DynamicArticle)

    ref = _compile_dynamic()
    gc.collect()
    assert ref() is None
    assert LongLivedOut in _PLAN_CACHE
    assert len(_PLAN_CACHE[LongLivedOut]) == 0


async def test_project_response_keeps_projected_item_unmaterialized(
    db: Database,
) -> None:
    class ArticleSlimOut(Schema):
        id: int
        title: str

    class ArticleFullSubOut(ArticleSlimOut):
        views: int

    with use_database(db):
        item = await ProjArticle.filter(title="First").aget_as(ArticleSlimOut)
        assert isinstance(item, _ProjectedItem)
        assert item._projected_handle is not None
        assert item._materialized is None

        # Exact schema match preserves the lazy _ProjectedItem without materializing
        kept = project_response(ArticleSlimOut, item)
        assert kept is item
        assert item._projected_handle is not None
        assert item._materialized is None

        # Subclass schema still strips subclass-only fields via dump(include=...)
        sub_item = await ProjArticle.filter(title="First").aget_as(ArticleFullSubOut)
        assert isinstance(sub_item, _ProjectedItem)
        stripped = project_response(ArticleSlimOut, sub_item)
        assert isinstance(stripped, dict)
        assert set(stripped) == {"id", "title"}
        assert "views" not in stripped


def test_select_sql_cache_hits_dialects_bounds_and_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qs_mod._SELECT_SQL_CACHE.pop(ProjArticle, None)
    calls = 0
    real_compile = core.compile_query

    def counting_compile(*args: Any, **kwargs: Any) -> tuple[str, list[Any]]:
        nonlocal calls
        calls += 1
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(core, "compile_query", counting_compile)

    # First compile is a miss; second with different bound value is a hit
    sql1, p1 = ProjArticle.filter(id=1).limit(2).compile(dialect="sqlite")
    sql2, p2 = ProjArticle.filter(id=99).limit(5).compile(dialect="sqlite")
    assert sql1 == sql2
    assert p1 == [1, 2]
    assert p2 == [99, 5]
    assert calls == 1

    # Different dialect produces dialect-specific placeholders (miss -> hit)
    pg_sql1, pg_p1 = ProjArticle.filter(id=1).limit(2).compile(dialect="postgres")
    pg_sql2, pg_p2 = ProjArticle.filter(id=2).limit(3).compile(dialect="postgres")
    assert pg_sql1 == pg_sql2
    assert pg_sql1 != sql1
    assert pg_p1 == [1, 2]
    assert pg_p2 == [2, 3]
    assert calls == 2

    # `contains` bypasses cache (pattern escaping depends on value & dialect)
    c1, cp1 = ProjArticle.filter(title__contains="foo").compile(dialect="sqlite")
    c2, cp2 = ProjArticle.filter(title__contains="bar").compile(dialect="sqlite")
    assert c1 == c2
    assert cp1 != cp2
    assert calls == 4

    # `in` distinguishes arity and empty list
    in0_sql, in0_p = ProjArticle.filter(id__in=[]).compile(dialect="sqlite")
    in2_sql, in2_p = ProjArticle.filter(id__in=[1, 2]).compile(dialect="sqlite")
    in2_sql2, in2_p2 = ProjArticle.filter(id__in=[3, 4]).compile(dialect="sqlite")
    assert in0_p == []
    assert in2_sql == in2_sql2
    assert in2_sql != in0_sql
    assert in2_p == [1, 2]
    assert in2_p2 == [3, 4]
    assert calls == 6

    # Bounded LRU eviction respects _SELECT_SQL_CACHE_MAX
    monkeypatch.setattr(qs_mod, "_SELECT_SQL_CACHE_MAX", 3)
    qs_mod._SELECT_SQL_CACHE.pop(ProjArticle, None)
    ProjArticle.filter(id=1).compile(dialect="sqlite")
    ProjArticle.filter(title="a").compile(dialect="sqlite")
    ProjArticle.filter(views=1).compile(dialect="sqlite")
    ProjArticle.filter(id=1, title="a").compile(dialect="sqlite")
    assert len(qs_mod._SELECT_SQL_CACHE[ProjArticle]) == 3

    # SQLite Decimal range refusal is never cached
    class DecimalItem(Model):
        id: int = field(primary_key=True)
        price: Decimal

    with pytest.raises(FieldError):
        DecimalItem.filter(price__gt=Decimal("1.00")).compile(dialect="sqlite")
    assert DecimalItem not in qs_mod._SELECT_SQL_CACHE


async def test_model_aget_as_fast_path_avoids_queryset_and_preserves_parity(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with use_database(db):
        # Warm plan & SQL caches
        warmed = await ProjArticle.aget_as(ArticleOut, id=1)
        assert isinstance(warmed, _ProjectedItem)
        assert warmed.title == "First"

        # Hot path allocates zero QuerySet instances
        def forbid_queryset(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("QuerySet should not be allocated on fast path")

        monkeypatch.setattr(QuerySet, "__init__", forbid_queryset)

        found = await ProjArticle.aget_as(ArticleOut, id=1)
        assert isinstance(found, _ProjectedItem)
        assert found._materialized is None
        assert found.id == 1
        assert found.title == "First"

        found_opt = await ProjArticle.aget_or_none_as(ArticleOut, id=2)
        assert found_opt is not None
        assert found_opt.title == "Second"

        assert await ProjArticle.aget_or_none_as(ArticleOut, id=999) is None
        with pytest.raises(ProjArticle.NotFound):
            await ProjArticle.aget_as(ArticleOut, id=999)
        with pytest.raises(MultipleObjectsReturned):
            await ProjArticle.aget_as(ArticleOut)
        with pytest.raises(MultipleObjectsReturned):
            await ProjArticle.aget_or_none_as(ArticleOut)

        # Multi-column equality also uses fast path
        multi = await ProjArticle.aget_as(ArticleOut, id=1, title="First")
        assert multi.id == 1

        monkeypatch.undo()

        # Complex filters, ordering, and prefetches still fall back cleanly
        filtered = await ProjArticle.aget_as(ArticleOut, id__gte=1, title="Second")
        assert filtered.id == 2
        with_pf = (
            await ProjArticle.filter(id=1)
            .select_related("author")
            .prefetch_related("tags")
            .aget_as(ArticleWithTagsOut)
        )
        assert with_pf.author is not None
        assert [t.label for t in with_pf.tags] == ["rust"]


async def test_projected_item_behaves_as_a_dataclass_instance(db: Database) -> None:
    with use_database(db):
        item = await ProjArticle.filter(id=1).aget_as(ArticleOut)
        assert type(cast(Any, item)) is _ProjectedItem
        assert dataclasses.is_dataclass(item)
        assert dataclasses.fields(item) == dataclasses.fields(ArticleOut)

        expected = dataclasses.asdict(
            ArticleOut.from_model(await ProjArticle.aget(id=1))
        )
        assert dataclasses.asdict(item) == expected

        changed = dataclasses.replace(item, views=99)
        assert type(changed) is ArticleOut
        assert changed.views == 99
        assert changed.title == "First"
        assert changed.id == 1

        # The lazy encoding survives the dataclass helpers on a fresh item.
        fresh = await ProjArticle.filter(id=2).aget_as(ArticleOut)
        assert dataclasses.is_dataclass(fresh)
        assert cast(Any, fresh)._projected_handle is not None


def test_the_bare_projected_class_has_no_dataclass_fields() -> None:
    assert dataclasses.fields(_ProjectedItem) == ()  # type: ignore[arg-type]


async def test_paginate_projected_skips_derive_and_preserves_stable_ordering(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with use_database(db):
        query = ProjArticle.all().order_by("-views")
        # Warm plan & SQL caches
        warmed = await query.apaginate_as(ArticleOut, PageParams(page=1, page_size=1))
        assert warmed.total == 4

        def forbid_derive(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("paginate_projected should not call QuerySet.derive")

        monkeypatch.setattr(QuerySet, "derive", forbid_derive)
        page = await query.apaginate_as(ArticleOut, PageParams(page=1, page_size=1))
        assert page.total == 4
        assert page.page == 1
        assert page.page_size == 1
        monkeypatch.undo()

        # Lazy materialization still produces the stably-ordered item
        assert [item.title for item in page.items] == ["Fourth"]
