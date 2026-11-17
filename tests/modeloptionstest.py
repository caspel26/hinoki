"""Model options as decorators: unique, indexed, ordered_by, labels."""

import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki import db
from hinoki.db import Database
from hinoki.errors import RegistrationError
from hinoki.orm import (
    Model,
    OnDelete,
    field,
    foreign_key,
    indexed,
    ordered_by,
    pk,
    table,
    timestamps,
    unique,
    use_database,
)
from hinoki.orm.migrations import index_name, plan, state_of

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")


@unique("owner", "url")
@indexed("created")
@indexed("owner", "created")
@ordered_by("-created", "id")
class Mark(Model, table="mo_marks", label="Mark", plural="Marks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner: str = field(max_length=20)
    url: str = field(max_length=100)
    created: datetime = field(default_factory=datetime.now)


def test_options_are_validated_when_applied() -> None:
    with pytest.raises(RegistrationError, match="unknown"):

        @unique("owner", "nope")
        class A(Model, table="mo_a"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner: str = ""

    with pytest.raises(RegistrationError, match="field\\(unique=True\\)"):

        @unique("owner")
        class B(Model, table="mo_b"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner: str = ""

    with pytest.raises(RegistrationError, match="twice"):

        @indexed("owner")
        @indexed("owner")
        class C(Model, table="mo_c"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner: str = ""

    with pytest.raises(RegistrationError, match=r"@unique.*twice"):

        @unique("owner", "value")
        @unique("owner", "value")
        class DuplicateUnique(Model, table="mo_duplicate_unique"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            owner: str = ""
            value: str = ""

    with pytest.raises(RegistrationError, match="needs a column"):
        ordered_by()


def test_timestamps_reject_non_callable_clock_and_unresolvable_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RegistrationError, match="clock must be callable"):

        @timestamps(clock=None)  # type: ignore[arg-type]
        class InvalidClock(Model, table="mo_invalid_clock"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            updated_at: datetime

    monkeypatch.setattr(
        "hinoki.orm.options.get_type_hints",
        lambda _model: (_ for _ in ()).throw(NameError("missing type")),
    )
    with pytest.raises(RegistrationError, match="cannot resolve timestamp"):

        @timestamps()
        class Unresolved(Model, table="mo_unresolved"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            updated_at: datetime


def test_timestamps_register_a_valid_datetime_field() -> None:
    @timestamps()
    class Valid(Model, table="mo_valid_timestamp"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        updated_at: datetime

    assert Valid.__updated_at__ == "updated_at"


def test_timestamps_reject_primary_key_and_non_datetime_fields() -> None:
    with pytest.raises(RegistrationError, match="cannot be the primary key"):

        @timestamps("id")
        class PrimaryKey(Model, table="mo_timestamp_pk"):
            id: datetime = field(primary_key=True, default_factory=datetime.now)

    with pytest.raises(RegistrationError, match="must be datetime"):

        @timestamps("value")
        class NonDatetime(Model, table="mo_timestamp_text"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            value: str


def test_the_options_are_visible_on_the_class() -> None:
    assert Mark.__unique__ == (("owner", "url"),)
    assert Mark.__indexes__ == (("created",), ("owner", "created"))
    assert Mark.__ordering__ == ("-created", "id")
    assert (Mark.__label__, Mark.__plural__) == ("Mark", "Marks")
    assert Mark.all().order == (("created", True), ("id", False))
    assert Mark.all().order_by("owner").order == (("owner", False),)
    assert Mark.all().compile("select")[0].count("ORDER BY") == 1


def test_pk_index_field_and_database_decorators() -> None:
    @db.indexed("title")
    @db.table("mo_decorated_records")
    class DecoratedRecord(Model):
        id: UUID = pk(default_factory=uuid4)
        title: str = field(index=True)

    assert DecoratedRecord.__table__ == "mo_decorated_records"
    assert DecoratedRecord.__primary_key__ == "id"
    assert DecoratedRecord(title="sample").__dict__["id"]
    assert DecoratedRecord.__indexes__ == (("title",),)
    assert state_of([DecoratedRecord])["tables"]["mo_decorated_records"]["indexes"] == [
        ["title"]
    ]


def test_table_decorator_rejects_conflicting_class_table() -> None:
    with pytest.raises(RegistrationError, match="conflicts with table="):

        @db.table("mo_new_name")
        class ConflictingTable(Model, table="mo_old_name"):
            id: int = field(primary_key=True)


def test_table_decorator_rejects_empty_names_and_abstract_models() -> None:
    with pytest.raises(RegistrationError, match="non-empty table name"):
        db.table("")

    with pytest.raises(RegistrationError, match="abstract model has no table"):

        @db.table("mo_abstract_records")
        class AbstractRecord(Model, abstract=True):
            id: int = field(primary_key=True)


def test_table_decorator_names_compiled_sql_and_foreign_key_targets() -> None:
    @db.table("mo_writers")
    class Writer(Model):
        id: int = pk()

    class Essay(Model, table="mo_essays"):
        id: int = pk()
        writer_id: int = foreign_key(Writer, on_delete=OnDelete.CASCADE)

    assert Writer.all().compile("select")[0] == 'SELECT "id" FROM "mo_writers"'
    joined, _ = Essay.all().select_related("writer").compile()
    assert 'JOIN "mo_writers"' in joined
    columns = state_of([Writer, Essay])["tables"]["mo_essays"]["columns"]
    assert columns["writer_id"]["references"]["table"] == "mo_writers"


def test_stacked_table_decorators_report_the_decorator_conflict() -> None:
    with pytest.raises(
        RegistrationError,
        match=r"@table\('mo_one'\) conflicts with @table\('mo_two'\)",
    ):

        @db.table("mo_one")
        @db.table("mo_two")
        class TwiceNamed(Model):
            id: int = pk()

    @db.table("mo_same")
    @db.table("mo_same")
    class SameName(Model):
        id: int = pk()

    assert SameName.__table__ == "mo_same"


def test_model_decorators_are_reexported_from_db() -> None:
    assert db.table is table
    assert db.indexed is indexed
    assert db.ordered_by is ordered_by
    with pytest.raises(AttributeError, match="no attribute 'tabel'"):
        _ = db.tabel  # type: ignore[attr-defined]


def test_index_field_rejects_non_bool_values() -> None:
    with pytest.raises(
        RegistrationError, match=r"index=\.\.\.\) must be True or False"
    ):
        field(index="yes")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="must be True or False"):
        field(index=1)  # type: ignore[arg-type]


def test_index_field_rejects_primary_and_unique_indexes() -> None:
    with pytest.raises(RegistrationError, match="already has an index"):
        field(primary_key=True, index=True)
    with pytest.raises(RegistrationError, match="already has an index"):
        field(unique=True, index=True)


def test_matching_decorator_still_replaces_inherited_indexes() -> None:
    @indexed("owner")
    class IndexedBase(Model, abstract=True):
        id: int = field(primary_key=True)
        owner: str = ""
        title: str = field(default="", index=True)

    @indexed("title")
    class IndexedChild(IndexedBase, table="mo_indexed_child"):
        pass

    assert IndexedChild.__indexes__ == (("title",),)


def test_field_index_is_inherited_through_an_undecorated_abstract_base() -> None:
    class IndexedTitle(Model, abstract=True):
        id: int = pk()
        title: str = field(default="", index=True)

    class Middle(IndexedTitle, abstract=True):
        body: str = ""

    class Leaf(Middle, table="mo_leaf"):
        pass

    @indexed("body")
    class DecoratedLeaf(Middle, table="mo_decorated_leaf"):
        pass

    assert Middle.__indexes__ == Leaf.__indexes__ == (("title",),)
    assert DecoratedLeaf.__indexes__ == (("body",), ("title",))
    assert state_of([Leaf])["tables"]["mo_leaf"]["indexes"] == [["title"]]


def test_redeclared_column_without_index_drops_the_field_index() -> None:
    class TitledBase(Model, abstract=True):
        id: int = pk()
        title: str = field(default="", index=True)
        body: str = ""

    class Plain(TitledBase, table="mo_plain"):
        title: str = ""

    @indexed("body")
    class Decorated(TitledBase, table="mo_decorated"):
        title: str = ""

    assert Plain.__indexes__ == ()
    assert Decorated.__indexes__ == (("body",),)


def test_redeclared_column_keeps_a_decorator_index_of_the_base() -> None:
    @indexed("title")
    class DecoratedTitleBase(Model, abstract=True):
        id: int = pk()
        title: str = field(default="", index=True)

    class Redeclared(DecoratedTitleBase, table="mo_redeclared"):
        title: str = ""

    assert Redeclared.__indexes__ == (("title",),)


def test_duplicate_decorator_for_field_index_is_rejected() -> None:
    with pytest.raises(RegistrationError, match=r"@indexed.*twice"):

        @indexed("title")
        @indexed("title")
        class DuplicateIndex(Model):
            id: int = field(primary_key=True)
            title: str = field(default="", index=True)


def test_the_planner_creates_and_changes_indexes(tmp_path: Path) -> None:
    first = plan([Mark], tmp_path, "initial")
    sql = (tmp_path / "0001-initial.sql").read_text()
    assert "UNIQUE (" in sql and sql.count("CREATE INDEX") == 2
    assert f'"{index_name("mo_marks", ["owner", "created"])}"' in sql
    assert first.operations
    assert state_of([Mark])["tables"]["mo_marks"]["indexes"] == [
        ["created"],
        ["owner", "created"],
    ]

    @indexed("url")
    @unique("owner", "url")
    class Mark2(Model, table="mo_marks"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        owner: str = field(max_length=20)
        url: str = field(max_length=100)
        created: datetime = field(default_factory=datetime.now)

    second = plan([Mark2], tmp_path, "indexes")
    text = " ".join(o.sql for o in second.operations)
    assert 'CREATE INDEX "ix_mo_marks_url"' in text and text.count("DROP INDEX") == 2
    assert not any(o.destructive for o in second.operations)


def test_long_index_names_are_shortened_deterministically() -> None:
    columns = ["a_very_long_column_name_number_one", "a_very_long_column_name_two"]
    name = index_name("a_table_with_a_rather_long_name", columns)
    assert len(name) <= 63 and name == index_name(
        "a_table_with_a_rather_long_name", columns
    )


@pytest.fixture
async def marks() -> AsyncIterator[Database]:
    assert URL is not None
    db = await Database.connect(URL, max_size=2)
    await db.execute("DROP TABLE IF EXISTS mo_marks")
    await db.execute(
        "CREATE TABLE mo_marks (id uuid primary key, owner varchar(20) not null, "
        "url varchar(100) not null, created timestamptz not null)"
    )
    with use_database(db):
        yield db
    await db.execute("DROP TABLE IF EXISTS mo_marks")
    db.close()


@needs_database
async def test_default_ordering_applies_to_queries_and_pages(marks: Database) -> None:
    base = datetime(2026, 1, 1).astimezone()
    for day in (1, 3, 2):
        await Mark.acreate(owner="a", url=f"u{day}", created=base.replace(day=day))
    rows = await Mark.all()
    assert [r.url for r in rows] == ["u3", "u2", "u1"]  # newest first, no order_by
    assert (await Mark.all().order_by("url"))[0].url == "u1"
    assert await Mark.all().acount() == 3
