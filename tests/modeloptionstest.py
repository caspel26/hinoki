"""Model options as decorators: unique, indexed, ordered_by, labels."""

import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki import RegistrationError
from hinoki.db import Database
from hinoki.orm import Model, field, indexed, ordered_by, unique, use_database
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

    with pytest.raises(RegistrationError, match="needs a column"):
        ordered_by()


def test_the_options_are_visible_on_the_class() -> None:
    assert Mark.__unique__ == (("owner", "url"),)
    assert Mark.__indexes__ == (("created",), ("owner", "created"))
    assert Mark.__ordering__ == ("-created", "id")
    assert (Mark.__label__, Mark.__plural__) == ("Mark", "Marks")
    assert Mark.all().order == (("created", True), ("id", False))
    assert Mark.all().order_by("owner").order == (("owner", False),)
    assert Mark.all().compile("select")[0].count("ORDER BY") == 1


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
    rows = await Mark.all().aall()
    assert [r.url for r in rows] == ["u3", "u2", "u1"]  # newest first, no order_by
    assert (await Mark.all().order_by("url").aall())[0].url == "u1"
    assert await Mark.all().acount() == 3
