"""Schemas derived from models: ``class PostOut(Schema, model=Post, ...)``."""

from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

import pytest

from hinoki import (
    UNSET,
    Field,
    Hinoki,
    Invalid,
    RegistrationError,
    Schema,
    ValidationError,
    field_check,
    http,
    object_check,
)
from hinoki.orm import Model, field, foreign_key
from hinoki.schema import graph_has_checks
from hinoki.testing import ASGIClient


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


class Settings2(Schema):
    colour: Annotated[str, Field(one_of=["red", "green"])]
    size: Annotated[int, Field(one_of=[1, 2, 3])]
    email: Annotated[str, Field(format="email")]
    page: Annotated[str, Field(format="url")] | None = None


def test_one_of_and_formats_run_in_the_engine() -> None:
    good = {"colour": "red", "size": 2, "email": "a.b+c@example.org"}
    assert Settings2.validate(good).size == 2
    codes = {
        (d.path, d.code)
        for bad in (
            {**good, "colour": "blue"},
            {**good, "size": 4},
            {**good, "size": True},
            {**good, "email": "nope"},
            {**good, "page": "ftp://x"},
        )
        for d in _problems(bad)
    }
    assert ("colour", "one_of") in codes and ("size", "one_of") in codes
    assert ("size", "type") in codes and ("email", "pattern") in codes
    assert ("page", "pattern") in codes


def _problems(data: dict[str, object]) -> list:  # type: ignore[type-arg]
    try:
        Settings2.validate(data)
    except ValidationError as error:
        return error.details
    return []


def test_one_of_and_format_mistakes_fail_at_class_creation() -> None:
    with pytest.raises(RegistrationError, match="one_of"):

        class A(Schema):
            n: Annotated[int, Field(one_of=["a"])]

    with pytest.raises(RegistrationError, match="do not apply"):

        class B(Schema):
            flag: Annotated[bool, Field(one_of=[True])]

    with pytest.raises(ValueError, match="unknown format"):
        Field(format="phone")
    with pytest.raises(ValueError, match="not both"):
        Field(format="email", pattern="x")


class Address(Schema):
    zip: str

    @field_check("zip")
    def five_digits(self, value: str) -> None:
        if not (len(value) == 5 and value.isdigit()):
            raise Invalid("zip", "must be five digits")


class SignUp(Schema):
    password: Annotated[str, Field(min_length=8)]
    again: str
    home: Address
    others: list[Address] = Field(default_factory=list)

    @object_check
    def same_password(self) -> None:
        if self.password != self.again:
            raise Invalid("mismatch", "the two passwords differ", path="again")

    @field_check("password")
    def not_common(self, value: str) -> None:
        if value == "password1":
            raise Invalid("common", "too common")


def _fail(data: object) -> set[tuple[str, str]]:
    try:
        SignUp.validate(data)
    except ValidationError as error:
        return {(d.path, d.code) for d in error.details}
    return set()


def test_checks_run_after_the_structure_and_report_everything_together() -> None:
    good = {"password": "s3cret-pass", "again": "s3cret-pass", "home": {"zip": "12345"}}
    assert not _fail(good)
    assert _fail({**good, "again": "other"}) == {("again", "mismatch")}
    assert _fail({**good, "password": "password1", "again": "x"}) == {
        ("password", "common"),
        ("again", "mismatch"),
    }
    nested = _fail(
        {**good, "home": {"zip": "1"}, "others": [{"zip": "22222"}, {"zip": "ab"}]}
    )
    assert nested == {("home.zip", "zip"), ("others[1].zip", "zip")}
    # structural problems come first and checks do not run on a malformed body
    assert _fail({**good, "password": "short"}) == {("password", "min_length")}


def test_checks_are_inherited_and_validated() -> None:
    class Strict(SignUp):
        @field_check("password")
        def no_spaces(self, value: str) -> None:
            if " " in value:
                raise Invalid("space", "no spaces")

    ok = {"password": "has space1", "again": "has space1", "home": {"zip": "12345"}}
    with pytest.raises(ValidationError) as raised:
        Strict.validate(ok)
    assert {d.code for d in raised.value.details} == {"space"}
    assert not _fail(ok)  # the parent does not have the child's check
    with pytest.raises(RegistrationError, match="at least one"):
        field_check()


def test_schemas_without_checks_pay_nothing() -> None:
    assert not graph_has_checks(PostOut) and graph_has_checks(SignUp)


async def test_checks_run_on_request_bodies_and_answer_422() -> None:
    @http.post("/signup", status=201)
    async def signup(data: SignUp) -> dict[str, str]:
        return {"ok": "yes"}

    app = Hinoki()
    app.include(signup)
    async with ASGIClient(app) as client:
        body = {"password": "s3cret-pass", "again": "nope", "home": {"zip": "12345"}}
        refused = await client.post("/signup", json=body)
        assert refused.status == 422
        first = refused.body["details"][0]
        assert (first["path"], first["code"]) == ("data.again", "mismatch")
        body["again"] = "s3cret-pass"
        assert (await client.post("/signup", json=body)).status == 201
