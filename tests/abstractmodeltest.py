"""Abstract models and mixins: inherited fields, options and relations."""

from __future__ import annotations

import os
import typing
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki import Schema, derive
from hinoki.admin import Admin
from hinoki.db import Database, UniqueViolation
from hinoki.errors import ModelError, RegistrationError
from hinoki.orm import (
    Model,
    OnDelete,
    field,
    foreign_key,
    indexed,
    migrator,
    ordered_by,
    unique,
    use_database,
)
from hinoki.orm.migrations import models_of, plan, state_of
from hinoki.resources import _CrudConfig
from tests.modelmixins import Keyed, Priced, Ranked, Stamped, Tracked

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")


class Team(Model, table="am_teams"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=40)


@unique("team_id", "slug")
class Owned(Model, abstract=True):
    team_id: UUID = foreign_key(
        Team, on_delete=OnDelete.CASCADE, related_name="{model}_set"
    )
    slug: str = field(max_length=40)


class Article(Keyed, Tracked, Owned, table="am_articles"):
    title: str = field(max_length=80)


class Page(Stamped, Ranked, Keyed, Owned, table="am_pages"):
    body: str = ""


class TrackedRead(Schema):
    updated_at: datetime


def test_a_plain_mixin_contributes_its_fields_and_methods() -> None:
    # Bases that are models or dataclasses come first in dataclass order (the
    # last base listed first), then the plain mixin's fields, then the class's.
    assert Page.__columns__ == (
        "team_id",
        "slug",
        "id",
        "rank",
        "created_at",
        "note",
        "body",
    )
    page = Page(team_id=uuid4(), slug="home", note="hello")  # type: ignore[call-arg]
    assert page.rank == 0 and page.created_at.tzinfo is not None
    assert page.describe().startswith("hello @ ")
    assert "registry" not in Page.__columns__  # ClassVar is not a field
    table = state_of([Team, Page])["tables"]["am_pages"]
    assert table["columns"]["created_at"]["type"] == "timestamptz"
    assert table["unique"] == [["team_id", "slug"]]


def test_a_mixin_annotation_keeps_the_meaning_it_had_in_its_own_module() -> None:
    class Offer(Priced, Keyed, table="am_offers"):
        label: str = ""

    # `Decimal` is not imported here: the mixin's string annotation was resolved
    # in the mixin's module before it became this model's.
    hints = typing.get_type_hints(Offer)
    assert hints["price"].__name__ == "Decimal"
    assert Offer().price == 0


def test_an_unresolvable_mixin_keeps_its_written_annotations() -> None:
    class Loose:
        cache: ClassVar[Missing] = ()  # type: ignore[name-defined]  # noqa: F821
        code: str = ""

    class Coded(Loose, Keyed, table="am_coded"):
        pass

    assert Coded.__columns__ == ("id", "code")  # the ClassVar is still not a field
    assert Coded(code="x").code == "x"  # type: ignore[call-arg]


def test_each_model_owns_the_field_objects_it_took_from_a_mixin() -> None:
    class Memo(Stamped, Keyed, table="am_memos"):
        text: str = ""

    assert Memo.__columns__ == ("id", "created_at", "note", "text")
    assert Memo(text="a").note == "" and Page(team_id=uuid4(), slug="s").note == ""
    assert Stamped.__dict__["created_at"] is not Memo.__dataclass_fields__["created_at"]


def test_an_abstract_model_has_no_table_and_cannot_be_queried() -> None:
    assert Tracked.__abstract__ and Tracked.__table__ == ""
    assert Tracked.__columns__ == ("updated_at",) and Tracked.__primary_key__ == ""
    assert Article.__abstract__ is False and Article.__primary_key__ == "id"
    with pytest.raises(ModelError, match="Tracked is abstract"):
        Tracked.all()
    with pytest.raises(ModelError, match="abstract"):
        Keyed.filter(id=uuid4())
    with pytest.raises(RegistrationError, match="abstract"):
        Admin(secret="s" * 32).register(Tracked)
    with pytest.raises(RegistrationError, match="Tracked is abstract"):
        _CrudConfig(model=Tracked, read=TrackedRead).check()
    assert Tracked not in models_of("tests.modelmixins")
    assert models_of("tests.abstractmodeltest")[:3] == [Team, Article, Page]


async def test_saving_an_abstract_instance_is_refused() -> None:
    with pytest.raises(ModelError, match="Keyed is abstract"):
        await Keyed().asave()


def test_abstract_and_table_rules_are_checked_at_declaration() -> None:
    with pytest.raises(RegistrationError, match="abstract model has no table"):

        class Both(Model, abstract=True, table="am_both"):
            id: int = field(primary_key=True)

    class Neither(Keyed):
        name: str = ""

    assert Neither.__table__ == "abstractmodeltest_neithers"
    assert Neither.__table_is_implicit__

    with pytest.raises(RegistrationError, match="exactly one primary_key"):

        class Keyless(Tracked, table="am_keyless"):
            name: str = ""


def test_options_are_inherited_from_every_abstract_base() -> None:
    assert Article.__columns__ == ("team_id", "slug", "updated_at", "id", "title")
    assert Article.__ordering__ == ("-updated_at",)
    assert Article.__updated_at__ == "updated_at"
    assert Article.__indexes__ == (("updated_at",),)
    assert Article.__unique__ == (("team_id", "slug"),)
    # The bases keep their own and do not see each other's.
    assert Tracked.__unique__ == () and Owned.__indexes__ == ()


def test_a_decorator_on_the_subclass_replaces_the_inherited_kind_only() -> None:
    @indexed("title")
    @indexed("slug")
    @ordered_by("title")
    class Story(Keyed, Tracked, Owned, table="am_stories"):
        title: str = ""

    assert Story.__indexes__ == (("title",), ("slug",))  # not ("updated_at",) too
    assert Story.__ordering__ == ("title",)
    assert Story.__unique__ == (("team_id", "slug"),)  # untouched kind: inherited
    assert Story.__updated_at__ == "updated_at"

    @indexed("updated_at")
    @indexed("title")
    class Kept(Keyed, Tracked, table="am_kept"):
        title: str = ""

    assert Kept.__indexes__ == (("updated_at",), ("title",))  # kept by repeating it


def test_a_replacement_in_an_abstract_base_holds_for_its_subclasses() -> None:
    @unique("code", "id")
    @indexed("code")
    class Coded(Model, abstract=True):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        code: str = ""
        slug: str = ""

    @unique("slug", "id")
    @indexed("slug")
    class Slugged(Coded, abstract=True):
        pass

    class Entry(Slugged, table="am_entries"):
        pass

    assert Slugged.__unique__ == (("slug", "id"),)
    assert Entry.__unique__ == (("slug", "id"),)  # not Coded's as well
    assert Entry.__indexes__ == (("slug",),)

    class Paired(Coded, abstract=True, unique=[("code", "slug")]):
        pass

    class Pair(Paired, table="am_pairs"):
        pass

    assert Pair.__unique__ == (("code", "slug"),)
    assert Pair.__indexes__ == (("code",),)  # the untouched kind is still inherited


def test_a_concrete_parent_does_not_pass_on_its_options() -> None:
    class Draft(Article, table="am_drafts"):
        reviewed: bool = False

    assert Draft.__columns__ == (*Article.__columns__, "reviewed")
    assert Draft.__ordering__ == () and Draft.__indexes__ == ()
    assert Draft.__unique__ == () and Draft.__updated_at__ is None
    assert sorted(Draft.__relations__) == ["team"]


def test_the_same_field_with_two_types_names_both_bases() -> None:
    class Short(Model, abstract=True):
        code: str = ""

    class Numbered:
        code: int = 0

    with pytest.raises(RegistrationError) as conflict:

        class Clash(Short, Numbered, Keyed, table="am_clash"):  # type: ignore[misc]
            pass

    message = str(conflict.value)
    assert "'code'" in message and "Short" in message and "Numbered" in message

    class Same:
        code: str = "x"

    class Agreed(Short, Same, Keyed, table="am_agreed"):
        pass

    assert Agreed().code == ""  # first base in the MRO supplies the default

    class Chosen(Short, Numbered, Keyed, table="am_chosen"):
        # The class decides.
        code: int = 7  # type: ignore[assignment]

    assert Chosen().code == 7

    class Wider(Short, abstract=True):
        # A subclass overriding its own parent is not a conflict.
        code: int = 1  # type: ignore[assignment]

    class Wide(Wider, Keyed, table="am_wide"):
        pass

    assert Wide().code == 1


def test_an_abstract_foreign_key_gives_each_model_its_own_reverse_name() -> None:
    assert Owned.__relations__ == {}
    assert sorted(Article.__relations__) == ["team"]
    assert {"article_set", "page_set"} <= set(Team.__relations__)
    assert Team.__relations__["article_set"].model is Article
    assert "{model}_set" not in Team.__relations__


def test_a_table_subclass_gets_its_own_placeholder_reverse_relation() -> None:
    class Group(Model, table="am_groups"):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Grouped(Model, abstract=True):
        group_id: UUID = foreign_key(
            Group, on_delete=OnDelete.CASCADE, related_name="{model}_set"
        )

    class Note(Grouped, Keyed, table="am_notes"):
        pass

    class Memo(Note, table="am_note_memos"):
        pass

    assert sorted(Group.__relations__) == ["memo_set", "note_set"]
    assert Group.__relations__["memo_set"].model is Memo
    assert sorted(Memo.__relations__) == ["group"]


def test_a_foreign_key_cannot_point_at_an_abstract_model() -> None:
    with pytest.raises(RegistrationError, match="Keyed is abstract"):

        class Pointer(Model, table="am_pointers"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            keyed_id: UUID = foreign_key(Keyed, on_delete=OnDelete.CASCADE)


def test_one_unresolvable_annotation_does_not_cause_a_false_conflict() -> None:
    class Cached:
        cache: ClassVar[Missing] = ()  # type: ignore[name-defined]  # noqa: F821
        code: str = ""

    class Coded(Model, abstract=True):
        code: str = ""

    class Item(Cached, Coded, Keyed, table="am_items"):
        pass

    assert Item.__columns__ == ("id", "code")


def test_a_fixed_reverse_name_shared_by_two_models_is_explained() -> None:
    class Target(Model, table="am_targets"):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Pointing(Model, abstract=True):
        target_id: UUID = foreign_key(
            Target, on_delete=OnDelete.CASCADE, related_name="pointers"
        )

    class First(Pointing, Keyed, table="am_first"):
        pass

    with pytest.raises(RegistrationError) as collision:

        class Second(Pointing, Keyed, table="am_second"):
            pass

    message = str(collision.value)
    assert "First.target_id" in message and "Second.target_id" in message
    assert "{model}" in message

    with pytest.raises(RegistrationError, match="placeholder"):

        class Odd(Keyed, table="am_odd"):
            target_id: UUID = foreign_key(
                Target, on_delete=OnDelete.CASCADE, related_name="{table}_set"
            )


def test_a_schema_can_be_derived_from_an_abstract_model() -> None:
    @derive(Tracked)
    class TrackedOut(Schema):
        pass

    assert list(TrackedOut.specs()) == ["updated_at"]


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    tables = ("am_articles", "am_pages", "am_teams", "hinoki_migrations")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    yield database
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


@needs_database
async def test_a_model_with_two_abstract_bases_migrates_and_round_trips(
    db: Database, tmp_path: Path
) -> None:
    result = plan([Team, Article, Page], tmp_path, "initial")
    described = " ".join(operation.description for operation in result.operations)
    assert "am_articles" in described and "am_pages" in described
    assert await migrator.apply(db, tmp_path) == ["0001-initial"]
    columns: list[dict[str, Any]] = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'am_articles' ORDER BY ordinal_position"
    )
    assert [row["column_name"] for row in columns] == list(Article.__columns__)
    indexes = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'am_articles'"
    )
    text = " ".join(row["indexdef"] for row in indexes)
    assert "(updated_at)" in text and "(team_id, slug)" in text
    with use_database(db):
        team = await Team.acreate(name="docs")
        first = await Article.acreate(team_id=team.id, slug="a", title="A")
        second = await Article.acreate(team_id=team.id, slug="b", title="B")
        with pytest.raises(UniqueViolation):
            await Article.acreate(team_id=team.id, slug="a", title="again")
        stamped = first.updated_at
        changed = await Article.aupdate(id=first.id, title="A2")
        assert changed.updated_at > stamped  # the inherited @timestamps column moved
        listed = await Article.all()
        assert [row.id for row in listed] == [first.id, second.id]  # -updated_at
        loaded = await Team.filter(id=team.id).prefetch_related("article_set").aget()
        assert {row.slug for row in loaded.related("article_set")} == {"a", "b"}
        page = await Page.acreate(team_id=team.id, slug="home", note="n")
        assert (await Page.aget(id=page.id)).created_at == page.created_at
