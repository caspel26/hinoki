"""Foreign keys: declaration, explicit loading, joins and the absence of lazy I/O."""

import os
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

import pytest

from hinoki.db import Database, ForeignKeyViolation
from hinoki.errors import (
    FieldError,
    MultipleObjectsReturned,
    RegistrationError,
    RelationNotLoaded,
)
from hinoki.orm import Model, OnDelete, field, foreign_key, use_database
from hinoki.orm.relations import ON_DELETE, Related, store

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


def make(
    name: str, bases: tuple[type, ...], namespace: dict[str, Any], table: str
) -> type[Model]:
    return cast(type[Model], type(name, bases, namespace, table=table))


class Writer(Model, table="rel_writers"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    articles: ClassVar["Related[list[Article]]"]
    profile: ClassVar["Related[Profile | None]"]


class Article(Model, table="rel_articles"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    writer_id: UUID = foreign_key(
        Writer, on_delete=OnDelete.RESTRICT, related_name="articles"
    )
    title: str
    writer: ClassVar[Related[Writer]]


class Note(Model, table="rel_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    article_id: UUID | None = foreign_key(
        Article, on_delete=OnDelete.SET_NULL, default=None
    )
    text: str = ""
    article: ClassVar[Related[Article | None]]


async def test_bulk_writes_reject_unsafe_or_joined_queries_before_database_access() -> (
    None
):
    with pytest.raises(FieldError, match="aupdate needs filters and values"):
        await Article.all().aupdate({"title": "x"})
    with pytest.raises(FieldError, match="aupdate needs filters and values"):
        await Article.filter(title="x").aupdate({})
    with pytest.raises(FieldError, match="adelete needs at least one condition"):
        await Article.all().adelete()
    with pytest.raises(FieldError, match="updates cannot filter across a relation"):
        await (
            Article.filter(writer__name="x")
            .select_related("writer")
            .aupdate({"title": "updated"})
        )
    with pytest.raises(FieldError, match="adelete cannot filter across a relation"):
        await Article.filter(writer__name="x").select_related("writer").adelete()


class Profile(Model, table="rel_profiles"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    writer_id: UUID = foreign_key(
        Writer,
        on_delete=OnDelete.CASCADE,
        related_name="profile",
        unique=True,
    )
    display_name: str
    writer: ClassVar[Related[Writer]]


def test_foreign_keys_register_both_directions() -> None:
    assert Article.__relations__["writer"].forward
    assert Writer.__relations__["articles"].model is Article
    assert not Writer.__relations__["articles"].forward
    assert "writer" not in Article.__columns__


def test_unique_foreign_key_registers_one_to_one_cardinality() -> None:
    assert Profile.__relations__["writer"].one_to_one
    assert Writer.__relations__["profile"].one_to_one
    assert Profile.__relations__["writer"].forward
    assert not Writer.__relations__["profile"].forward


def test_relations_are_never_loaded_by_attribute_access() -> None:
    article = Article(writer_id=uuid4(), title="t")
    with pytest.raises(RelationNotLoaded, match=r"select_related\('writer'\)"):
        _ = article.writer
    with pytest.raises(RelationNotLoaded, match=r"prefetch_related\('articles'\)"):
        _ = Writer(name="n").articles
    with pytest.raises(FieldError):
        article.related("nope")


def test_declaration_errors() -> None:
    with pytest.raises(RegistrationError, match=r"OnDelete\.RESTRICT"):
        foreign_key(Writer, on_delete="cascade")  # type: ignore[arg-type]
    assert [rule.value for rule in OnDelete] == list(ON_DELETE)
    for annotation in (UUID, "UUID"):
        with pytest.raises(RegistrationError, match=r"Bad\.writer_id.*SET_NULL"):
            type(
                "Bad",
                (Model,),
                {
                    "__annotations__": {"id": int, "writer_id": annotation},
                    "id": field(primary_key=True),
                    "writer_id": foreign_key(Writer, on_delete=OnDelete.SET_NULL),
                },
                table="rel_bad_null",
            )
    assert "bad" not in Writer.__relations__  # a refused model attaches nothing
    with pytest.raises(RegistrationError, match="_id"):
        type(
            "Bad",
            (Model,),
            {
                "__annotations__": {"id": int, "owner": UUID},
                "id": field(primary_key=True),
                "owner": foreign_key(Writer, on_delete=OnDelete.CASCADE),
            },
            table="bad",
        )
    with pytest.raises(RegistrationError, match="already has"):
        type(
            "Clash",
            (Model,),
            {
                "__annotations__": {"id": int, "writer_id": UUID},
                "id": field(primary_key=True),
                "writer_id": foreign_key(
                    Writer, on_delete=OnDelete.CASCADE, related_name="name"
                ),
            },
            table="clash",
        )


def test_select_related_compiles_one_join() -> None:
    sql, params = Article.all().select_related("writer").compile()
    assert sql == (
        'SELECT "rel_articles"."id", "rel_articles"."writer_id", '
        '"rel_articles"."title", "rel_writer"."id" AS "rel_writer__0", '
        '"rel_writer"."name" AS "rel_writer__1" '
        'FROM "rel_articles" LEFT JOIN "rel_writers" AS "rel_writer" '
        'ON "rel_writer"."id" = "rel_articles"."writer_id"'
    )
    assert params == []


def test_a_count_does_not_join_a_relation_only_selected_for_loading() -> None:
    loaded = Article.filter(title="t").select_related("writer")
    sql, params = loaded.derive(order=(), limit_to=None, offset_by=None).compile(
        "count"
    )
    assert "JOIN" not in sql and params == ["t"]
    filtered = Article.filter(writer__name="x").select_related("writer")
    counted, _ = filtered.compile("count")
    assert 'LEFT JOIN "rel_writers" AS "rel_writer"' in counted  # the filter needs it
    assert 'AS "rel_writer__' not in counted


def test_filtering_across_a_relation_joins_without_selecting() -> None:
    sql, params = Article.filter(writer__name__ne="x", title="t").compile()
    assert 'LEFT JOIN "rel_writers" AS "rel_writer"' in sql
    assert '"rel_writer"."name" <> $1 AND "rel_articles"."title" = $2' in sql
    assert 'AS "rel_writer__' not in sql
    assert params == ["x", "t"]


@pytest.mark.parametrize(
    "build",
    [
        lambda: Article.all().select_related("nope"),
        lambda: Article.all().prefetch_related("nope"),
        lambda: Writer.all().select_related("articles"),
        lambda: Writer.filter(articles__title="x"),
        lambda: Article.filter(writer__nope=1),
    ],
)
def test_unknown_or_unsupported_relations_are_rejected(build: Any) -> None:
    with pytest.raises(FieldError):
        build()


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = Database.fetch
    original_models = Database.fetch_models

    async def counting(self: Database, sql: str, *params: Any) -> Any:
        seen.append(sql)
        return await original(self, sql, *params)

    async def counting_models(self: Database, sql: str, *params: Any) -> Any:
        seen.append(sql)
        return await original_models(self, sql, *params)

    monkeypatch.setattr(Database, "fetch", counting)
    monkeypatch.setattr(Database, "fetch_models", counting_models)
    return seen


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=3, wait=0.5)
    tables = ("rel_notes", "rel_articles", "rel_profiles", "rel_writers")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await database.execute(
        "CREATE TABLE rel_writers (id uuid PRIMARY KEY, name text NOT NULL)"
    )
    await database.execute(
        "CREATE TABLE rel_articles (id uuid PRIMARY KEY, writer_id uuid NOT NULL "
        "REFERENCES rel_writers(id) ON DELETE RESTRICT, title text NOT NULL)"
    )
    await database.execute(
        "CREATE TABLE rel_notes (id uuid PRIMARY KEY, article_id uuid "
        "REFERENCES rel_articles(id) ON DELETE SET NULL, text text NOT NULL)"
    )
    await database.execute(
        "CREATE TABLE rel_profiles (id uuid PRIMARY KEY, writer_id uuid NOT NULL "
        "REFERENCES rel_writers(id) ON DELETE CASCADE, display_name text NOT NULL, "
        "CONSTRAINT rel_profiles_writer_id_unique UNIQUE (writer_id))"
    )
    with use_database(database):
        yield database
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


@needs_database
async def test_select_related_loads_in_one_query(
    db: Database, statements: list[str]
) -> None:
    ada = await Writer.acreate(name="Ada")
    await Article.acreate(writer_id=ada.id, title="One")
    statements.clear()
    [article] = await Article.all().select_related("writer")
    assert len(statements) == 1
    assert article.writer.name == "Ada"
    assert article.related("writer") is article.writer


@needs_database
async def test_prefetch_related_uses_one_extra_query_per_relation(
    db: Database, statements: list[str]
) -> None:
    ada = await Writer.acreate(name="Ada")
    bob = await Writer.acreate(name="Bob")
    for index in range(3):
        await Article.acreate(writer_id=ada.id, title=f"a{index}")
    statements.clear()
    writers = await Writer.all().order_by("name").prefetch_related("articles")
    assert len(statements) == 2
    assert [len(w.articles) for w in writers] == [3, 0]
    assert {a.title for a in writers[0].articles} == {"a0", "a1", "a2"}
    assert writers[1].id == bob.id

    statements.clear()
    articles = await Article.all().prefetch_related("writer")
    assert len(statements) == 2
    assert {a.writer.name for a in articles} == {"Ada"}


@needs_database
async def test_one_to_one_prefetch_is_batched_and_returns_none_when_absent(
    db: Database, statements: list[str]
) -> None:
    ada = await Writer.acreate(name="Ada")
    bob = await Writer.acreate(name="Bob")
    profile = await Profile.acreate(writer_id=ada.id, display_name="Ada L.")

    statements.clear()
    writers = await Writer.all().order_by("name").prefetch_related("profile")
    assert len(statements) == 2
    assert [writer.profile for writer in writers] == [profile, None]
    assert [writer.id for writer in writers] == [ada.id, bob.id]

    statements.clear()
    loaded = await Profile.all().select_related("writer").aget()
    assert len(statements) == 1
    assert loaded.writer.id == ada.id


@needs_database
async def test_one_to_one_prefetch_rejects_duplicate_rows_when_constraint_is_missing(
    db: Database,
) -> None:
    writer = await Writer.acreate(name="Ada")
    await db.execute(
        "ALTER TABLE rel_profiles DROP CONSTRAINT rel_profiles_writer_id_unique"
    )
    await Profile.acreate(writer_id=writer.id, display_name="first")
    await Profile.acreate(writer_id=writer.id, display_name="second")

    with pytest.raises(MultipleObjectsReturned, match="expected at most one Profile"):
        await Writer.all().prefetch_related("profile").aget()


@needs_database
async def test_filter_across_relation_and_nullable_keys(db: Database) -> None:
    ada = await Writer.acreate(name="Ada")
    bob = await Writer.acreate(name="Bob")
    article = await Article.acreate(writer_id=ada.id, title="Mine")
    await Article.acreate(writer_id=bob.id, title="Theirs")
    found = await Article.filter(writer__name="Ada")
    assert [a.id for a in found] == [article.id]
    assert await Article.filter(writer__name__in=["Ada", "Bob"]).acount() == 2

    orphan = await Note.acreate(text="loose")
    attached = await Note.acreate(article_id=article.id, text="tied")
    notes = await Note.all().select_related("article").order_by("text")
    assert [n.id for n in notes] == [orphan.id, attached.id]
    assert notes[0].article is None
    assert notes[1].article is not None
    assert notes[1].article.title == "Mine"
    prefetched = await Note.filter(id=orphan.id).prefetch_related("article")
    assert prefetched[0].article is None


@needs_database
async def test_select_related_with_paging_keeps_total_and_rows(db: Database) -> None:
    ada = await Writer.acreate(name="Ada")
    for index in range(5):
        await Article.acreate(writer_id=ada.id, title=f"t{index}")
    query = Article.all().select_related("writer").order_by("title")
    assert await query.acount() == 5
    page = await query.limit(2).offset(2)
    assert [a.title for a in page] == ["t2", "t3"]
    assert all(a.writer.name == "Ada" for a in page)


@needs_database
async def test_database_enforces_the_declared_rule(db: Database) -> None:
    ada = await Writer.acreate(name="Ada")
    await Article.acreate(writer_id=ada.id, title="x")
    with pytest.raises(ForeignKeyViolation):
        await Writer.adelete(id=ada.id)
    with pytest.raises(ForeignKeyViolation):
        await Article.acreate(writer_id=uuid4(), title="orphan")


def test_a_field_named_like_a_model_method_is_refused() -> None:
    for reserved in (
        "remember",
        "insert",
        "asave",
        "get",
        "changes",
        "is_new",
        "is_dirty",
        "was_changed",
        "after_commit",
        "before_save",
    ):
        with pytest.raises(RegistrationError, match="collide"):
            type(
                "Clashing",
                (Model,),
                {
                    "__annotations__": {"id": int, reserved: str},
                    "id": field(primary_key=True),
                },
                table="clashing",
            )


def test_assigning_a_relation_is_refused() -> None:
    article = Article(writer_id=uuid4(), title="t")
    with pytest.raises(AttributeError, match="assign writer_id"):
        article.writer = Writer(name="x")  # type: ignore[misc]


def test_models_with_foreign_keys_can_be_subclassed_and_redefined() -> None:
    child = cast(type[Model], type("Child", (Article,), {}, table="rel_children"))
    assert child.__relations__["writer"].forward
    assert set(Writer.__relations__) == {"articles", "profile"}

    owner = make(
        "Owner",
        (Model,),
        {"__annotations__": {"id": int}, "id": field(primary_key=True)},
        table="rel_owners",
    )

    def define() -> type[Model]:
        return make(
            "Pet",
            (Model,),
            {
                "__annotations__": {"id": int, "owner_id": int},
                "id": field(primary_key=True),
                "owner_id": foreign_key(
                    owner, on_delete=OnDelete.CASCADE, related_name="pets"
                ),
            },
            table="rel_pets",
        )

    first, second = define(), define()  # a reload defines the same model again
    assert owner.__relations__["pets"].model is second
    assert first is not second
    with pytest.raises(RegistrationError, match="already has"):
        type(
            "Cat",
            (Model,),
            {
                "__annotations__": {"id": int, "owner_id": int},
                "id": field(primary_key=True),
                "owner_id": foreign_key(
                    owner, on_delete=OnDelete.CASCADE, related_name="pets"
                ),
            },
            table="rel_cats",
        )


def test_a_failed_definition_does_not_leave_half_attached_relations() -> None:
    before = dict(Writer.__relations__)
    with pytest.raises(RegistrationError):
        type(
            "Broken",
            (Model,),
            {
                "__annotations__": {"id": int, "writer_id": UUID, "owner_id": UUID},
                "id": field(primary_key=True),
                "writer_id": foreign_key(
                    Writer, on_delete=OnDelete.CASCADE, related_name="broken_set"
                ),
                "owner_id": foreign_key(
                    Writer, on_delete=OnDelete.CASCADE, related_name="articles"
                ),
            },
            table="broken",
        )
    assert Writer.__relations__ == before


def test_long_column_names_do_not_overflow_aliases() -> None:
    long = make(
        "Long",
        (Model,),
        {
            "__annotations__": {
                "id": int,
                "display_name_shown_on_the_public_profile": str,
            },
            "id": field(primary_key=True),
        },
        table="rel_long",
    )
    holder = make(
        "Holder",
        (Model,),
        {
            "__annotations__": {"id": int, "primary_author_of_the_book_id": int},
            "id": field(primary_key=True),
            "primary_author_of_the_book_id": foreign_key(
                long, on_delete=OnDelete.CASCADE
            ),
        },
        table="rel_holder",
    )
    sql, _ = holder.all().select_related("primary_author_of_the_book").compile()
    assert "display_name_shown" in sql
    assert '"rel_primary_author_of_the_book__1"' in sql


def test_a_join_alias_equal_to_the_base_table_is_refused() -> None:
    odd = make(
        "Odd",
        (Model,),
        {
            "__annotations__": {"id": int, "writer_id": UUID},
            "id": field(primary_key=True),
            "writer_id": foreign_key(Writer, on_delete=OnDelete.CASCADE),
        },
        table="rel_writer",
    )
    with pytest.raises(ValueError, match="equals the base table"):
        odd.all().select_related("writer").compile()


@needs_database
async def test_a_loaded_relation_is_not_served_after_its_key_changed(
    db: Database,
) -> None:
    ada = await Writer.acreate(name="Ada")
    bob = await Writer.acreate(name="Bob")
    await Article.acreate(writer_id=ada.id, title="t")
    [article] = await Article.all().select_related("writer")
    assert article.writer.name == "Ada"
    article.writer_id = bob.id
    with pytest.raises(RelationNotLoaded, match="another writer_id"):
        _ = article.writer
    await article.asave()
    with pytest.raises(RelationNotLoaded):
        _ = article.writer


@needs_database
async def test_delete_across_a_relation_is_refused_clearly(db: Database) -> None:
    with pytest.raises(FieldError, match="across a relation"):
        await Article.adelete(writer__name="Ada")


def test_a_forward_relation_loaded_in_memory_goes_stale_when_its_key_changes() -> None:
    ada = Writer(name="Ada")
    article = Article(writer_id=ada.id, title="t")
    store(article, "writer", ada)
    assert article.writer is ada

    article.writer_id = uuid4()
    with pytest.raises(RelationNotLoaded, match="another writer_id"):
        _ = article.writer


def test_a_forward_relation_loaded_as_none_goes_stale_when_a_key_is_set() -> None:
    note = Note(article_id=None)
    store(note, "article", None)
    assert note.article is None

    note.article_id = uuid4()
    with pytest.raises(RelationNotLoaded, match="another article_id"):
        _ = note.article
