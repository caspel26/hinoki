"""Schemas derived from models: ``class PostOut(Schema, model=Post, ...)``."""

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

import pytest

from hinoki import UNSET, Field, RegistrationError, Schema, ValidationError
from hinoki.orm import Model, field, foreign_key


class Author(Model, table="ds_authors"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=40)


class Post(Model, table="ds_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(Author, on_delete="restrict", related_name="posts")
    title: str = field(max_length=60)
    slug: str = field(unique=True, max_length=80)
    body: str
    published: bool = False
    note: str | None = None
    created_at: datetime = field(default_factory=datetime.now)


class PostCreate(Schema, model=Post, fields=["title", "slug", "body", "published"]):
    pass


class PostOut(Schema, model=Post, exclude=["note"]):
    author_name: str


class PostPatch(Schema, model=Post, fields=["title", "body", "note"], optional=True):
    pass


class Overridden(Schema, model=Post, fields=["title", "body"]):
    title: Annotated[str, Field(min_length=3, max_length=10)]


def test_columns_become_fields_with_model_limits_and_defaults() -> None:
    specs = PostCreate.specs()
    assert list(specs) == ["title", "slug", "body", "published"]
    assert specs["title"].required and not specs["published"].required
    ok = PostCreate.validate({"title": "t", "slug": "s", "body": "b"})
    assert ok.published is False  # type: ignore[attr-defined]  # model default
    with pytest.raises(ValidationError) as raised:
        PostCreate.validate({"title": "x" * 61, "slug": "s", "body": "b"})
    assert raised.value.details[0].code == "max_length"  # from field(max_length=60)


def test_exclude_adds_declared_fields_and_keeps_nullable_types() -> None:
    specs = PostOut.specs()
    assert "note" not in specs and "author_name" in specs and "id" in specs
    shown = PostOut(  # type: ignore[call-arg]  # derived fields are not visible to mypy
        id=uuid4(),
        author_id=uuid4(),
        title="t",
        slug="s",
        body="b",
        published=True,
        created_at=datetime.now(),
        author_name="Ann",
    )
    assert shown.dump()["author_name"] == "Ann"
    assert PostPatch.validate({"note": None}).to_dict() == {"note": None}  # nullable


def test_optional_gives_the_patch_shape() -> None:
    nothing = PostPatch.validate({})
    assert nothing.title is UNSET  # type: ignore[attr-defined]
    assert nothing.to_dict() == {}
    some = PostPatch.validate({"title": "new"})
    assert some.to_dict() == {"title": "new"}
    with pytest.raises(ValidationError):
        PostPatch.validate({"title": 5})


def test_declared_fields_override_derived_ones() -> None:
    with pytest.raises(ValidationError):
        Overridden.validate({"title": "ab", "body": "b"})  # min_length=3 from the body
    assert Overridden.validate({"title": "abc", "body": "b"}).title == "abc"


def test_mistakes_fail_when_the_class_is_created() -> None:
    with pytest.raises(RegistrationError, match="no columns"):

        class A(Schema, model=Post, fields=["nope"]):
            pass

    with pytest.raises(RegistrationError, match="not both"):

        class B(Schema, model=Post, fields=["title"], exclude=["body"]):
            pass

    with pytest.raises(RegistrationError, match="unknown fields"):

        class C(Schema, model=Post, fields=["title"], optional=["body"]):
            pass

    with pytest.raises(RegistrationError, match="need model"):

        class D(Schema, fields=["title"]):
            pass

    with pytest.raises(RegistrationError, match="Model class"):

        class E(Schema, model=int):
            pass


def test_derived_schemas_work_with_the_native_engine_and_the_model() -> None:
    created = PostCreate.validate({"title": "t", "slug": "s", "body": "b"})
    row = Post.from_schema(created, author_id=uuid4())
    assert (row.title, row.slug, row.published) == ("t", "s", False)
    assert PostOut.native_schema() is not None
