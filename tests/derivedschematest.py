"""Schemas derived from models with ``@derive(Model, ...)``."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from types import CellType, FunctionType, SimpleNamespace
from typing import Annotated, Any, cast, get_type_hints
from uuid import UUID, uuid4

import pytest

from hinoki import (
    UNSET,
    Field,
    Hinoki,
    Invalid,
    Response,
    Schema,
    SchemaConfig,
    SchemaExtra,
    derive,
    field_check,
    http,
    object_check,
)
from hinoki.db import Database, DatabaseModule
from hinoki.derive import _class_cell as find_class_cell
from hinoki.derive import rebuild
from hinoki.errors import RegistrationError, ValidationError
from hinoki.orm import Model, OnDelete, field, foreign_key, use_database
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.pagination import Page
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import Pipeline, project_response
from hinoki.runtime.responsecheck import ResponseContractError
from hinoki.schema import graph_has_checks
from hinoki.testing import ASGIClient


class Author(Model, table="ds_authors"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=40)


class Post(Model, table="ds_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    author_id: UUID = foreign_key(
        Author, on_delete=OnDelete.RESTRICT, related_name="posts"
    )
    title: str = field(max_length=60)
    slug: str = field(unique=True, max_length=80)
    body: str
    published: bool = False
    note: str | None = None
    created_at: datetime = field(default_factory=datetime.now)


@derive(Post, only=("title", "slug", "body", "published"))
class PostCreate(Schema):
    pass


@derive(Post, exclude=("note",))
class PostOut(Schema):
    author_name: str


@derive(Post, only=("title", "body", "note"), partial=True)
class PostPatch(Schema):
    pass


@derive(Post, only=("title", "body"))
class Overridden(Schema):
    title: Annotated[str, Field(min_length=3, max_length=10)]


@derive(Post, only=("title",))
class IgnoreConfiguredDerived(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    pass


class IgnoreBase(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    pass


@derive(Post, only=("title",))
class ExplicitForbidDerived(IgnoreBase, config=SchemaConfig()):
    pass


def post_row(title: str = "title") -> Post:
    return Post(
        id=uuid4(),
        author_id=uuid4(),
        title=title,
        slug=f"{title}-slug",
        body="body",
        published=True,
        created_at=datetime.now(UTC),
    )


def test_from_models_eagerly_maps_derived_and_partial_schemas() -> None:
    rows = (post_row(title) for title in ("first", "second"))

    outputs = PostOut.from_models(rows, author_name="A. Example")
    patches = PostPatch.from_models([post_row("partial")])

    assert isinstance(outputs, list)
    assert [item.title for item in outputs] == ["first", "second"]  # type: ignore[attr-defined]
    assert [item.author_name for item in outputs] == ["A. Example", "A. Example"]
    assert "note" not in outputs[0].dump()
    assert patches[0].title == "partial"  # type: ignore[attr-defined]
    assert PostOut.from_models(iter(())) == []


def test_from_models_preserves_from_model_duck_typing_and_errors() -> None:
    row = post_row()
    shaped = SimpleNamespace(
        id=row.id,
        author_id=row.author_id,
        title=row.title,
        slug=row.slug,
        body=row.body,
        published=row.published,
        created_at=row.created_at,
    )

    assert PostOut.from_models([shaped], author_name="A. Example")[0].title == "title"  # type: ignore[attr-defined]
    with pytest.raises(
        AttributeError, match=r"PostOut\.id: object has no attribute 'id'"
    ):
        PostOut.from_models([object()])


def test_from_models_stops_at_first_conversion_error() -> None:
    consumed: list[int] = []

    def rows() -> Iterator[object]:
        for number, row in enumerate((post_row(), object(), post_row()), start=1):
            consumed.append(number)
            yield row

    with pytest.raises(AttributeError):
        PostOut.from_models(rows(), author_name="A. Example")
    assert consumed == [1, 2]


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
        created_at=datetime.now(UTC),
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


def test_mistakes_fail_when_the_class_is_declared_or_first_used() -> None:
    with pytest.raises(RegistrationError, match="no columns"):

        @derive(Post, only=("nope",))
        class A(Schema):
            pass

    with pytest.raises(RegistrationError, match="only and exclude"):

        @derive(Post, only=("title",), exclude=("body",))
        class B(Schema):
            pass

    with pytest.raises(RegistrationError, match="unknown fields"):

        @derive(Post, only=("title",), partial=("body",))
        class C(Schema):
            pass

    with pytest.raises(TypeError, match="model"):

        class E(Schema, model=Post):
            pass

    with pytest.raises(RegistrationError, match="source must be a Model class"):

        @derive(str)  # type: ignore[arg-type]
        class InvalidSource(Schema):
            pass

    with pytest.raises(TypeError, match="missing 1 required positional argument"):

        @derive(only=("title",))  # type: ignore[call-arg]
        class D(Schema):
            pass


def test_without_a_selection_every_column_is_taken_and_partial_can_name_fields() -> (
    None
):
    @derive(Post)
    class Everything(Schema):
        pass

    assert list(Everything.specs()) == list(Post.__columns__)

    @derive(Post, only=("title", "body"), partial=("body",))
    class Some(Schema):
        pass

    assert Some.specs()["title"].required and not Some.specs()["body"].required


def test_derive_is_one_callable_with_tuple_options() -> None:
    assert callable(derive)
    assert not any(
        hasattr(derive, name) for name in ("model", "only", "exclude", "partial")
    )
    with pytest.raises(RegistrationError, match="selects no columns"):
        derive(Post, only=())

    @derive(Post, exclude=())
    class EmptyExclusion(Schema):
        pass

    assert list(EmptyExclusion.specs()) == list(Post.__columns__)


def test_derive_model_annotation_resolves_for_runtime_introspection() -> None:
    assert get_type_hints(derive)["model"] == type[Model]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"only": ["title"]}, "only must be a tuple"),
        ({"exclude": ["title"]}, "exclude must be a tuple"),
        ({"only": (1,)}, "field names must be strings"),
        ({"partial": 1}, "partial must be bool or a tuple"),
        ({"partial": (1,)}, "field names must be strings"),
    ],
)
def test_derive_rejects_invalid_option_shapes(
    options: dict[str, object], message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        derive(Post, **options)  # type: ignore[arg-type]


class DescriptorBase(Schema):
    @property
    def label(self) -> str:
        return "base"

    @classmethod
    def category(cls) -> str:
        return "base"


@derive(Post, only=("title",))
class DescriptorDerived(DescriptorBase):
    @property
    def label(self) -> str:
        return f"{super().label} derived"

    @classmethod
    def category(cls) -> str:
        return f"{super().category()} derived"

    @staticmethod
    def name() -> str:
        return cast(str, __class__.__name__)  # type: ignore[name-defined]

    @cached_property
    def cached_label(self) -> str:
        return f"{super().label} cached"


def test_derive_preserves_class_cells_in_descriptors() -> None:
    item = DescriptorDerived(title="test")  # type: ignore[call-arg]
    assert item.label == "base derived"
    assert item.cached_label == "base cached"
    assert DescriptorDerived.category() == "base derived"
    assert DescriptorDerived.name() == "DescriptorDerived"


def _descriptor_function_with_cell(cell: CellType) -> FunctionType:
    descriptor = DescriptorDerived.__source__["name"]
    function = cast(Any, descriptor).__func__
    return FunctionType(
        function.__code__, function.__globals__, function.__name__, closure=(cell,)
    )


def _make_cell(value: object) -> CellType:
    def capture() -> object:
        return value

    closure = capture.__closure__
    assert closure is not None
    return closure[0]


def test_derive_rejects_two_distinct_class_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _descriptor_function_with_cell(_make_cell(DescriptorDerived))
    second = _descriptor_function_with_cell(_make_cell(DescriptorDerived))
    monkeypatch.setattr(
        DescriptorDerived,
        "__source__",
        {"first": first, "second": second},
    )

    with pytest.raises(RegistrationError, match="multiple __class__ cells"):
        rebuild(DescriptorDerived, (Post, None, (), False))


def test_derive_ignores_an_empty_class_cell() -> None:
    descriptor = DescriptorDerived.__source__["name"]
    function = cast(Any, descriptor).__func__
    cell = _make_cell(DescriptorDerived)
    del cell.cell_contents
    empty = FunctionType(
        function.__code__, function.__globals__, function.__name__, closure=(cell,)
    )

    assert find_class_cell(empty, DescriptorDerived) is None


def test_field_pattern_that_python_cannot_compile_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hinoki.fieldtypes.core.check_pattern", lambda _pattern: None)
    with pytest.raises(RegistrationError, match="invalid pattern"):

        class InvalidPattern(Schema):
            value: Annotated[str, Field(pattern="(")]


def test_checks_and_methods_in_the_class_body_survive_the_derivation() -> None:
    @derive(Post, only=("title", "body"))
    class Checked(Schema):
        @field_check("title")
        def not_shouting(self, value: str) -> None:
            if value.isupper():
                raise Invalid("shouting", "no shouting")

        def shout(self) -> str:
            return str(self.title).upper()  # type: ignore[attr-defined]

    assert Checked.validate({"title": "ok", "body": "b"}).shout() == "OK"
    with pytest.raises(ValidationError):
        Checked.validate({"title": "LOUD", "body": "b"})


def test_derived_schemas_work_with_the_native_engine_and_the_model() -> None:
    created = PostCreate.validate({"title": "t", "slug": "s", "body": "b"})
    row = Post.from_schema(created, author_id=uuid4())
    assert (row.title, row.slug, row.published) == ("t", "s", False)
    assert PostOut.native_schema() is not None


def test_derive_rebuild_preserves_explicit_class_config() -> None:
    assert IgnoreConfiguredDerived.__schema_config__.extra is SchemaExtra.IGNORE
    assert IgnoreConfiguredDerived.validate({"title": "t", "vendor": 1}).title == "t"  # type: ignore[attr-defined]

    assert ExplicitForbidDerived.__schema_config__.extra is SchemaExtra.FORBID
    with pytest.raises(ValidationError) as rejected:
        ExplicitForbidDerived.validate({"title": "t", "vendor": 1})
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("vendor", "extra_forbidden")
    ]


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


class SchemaLabel(Schema):
    def label(self) -> str:
        return "base"


@derive(Post, only=("title", "slug", "body", "published"))
class DerivedLabel(SchemaLabel):
    def label(self) -> str:
        return f"{super().label()} derived"


def test_derived_schema_methods_keep_zero_argument_super() -> None:
    instance = DerivedLabel(  # type: ignore[call-arg]  # fields are derived at runtime
        title="x", slug="x", body="x", published=False
    )
    assert instance.label() == "base derived"


@derive(Post, only=("title",))
class AutomaticPostOut(Schema):
    title: str


async def test_handler_returning_model_fails_response_contract_on_both_lanes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @http.get("/auto")
    def synchronous() -> AutomaticPostOut:
        return post_row("sync-leak")  # type: ignore[return-value]

    @http.get("/auto-async")
    async def asynchronous() -> AutomaticPostOut:
        return post_row("async-leak")  # type: ignore[return-value]

    @http.get("/wrong")
    def wrong() -> AutomaticPostOut:
        return Author(id=uuid4(), name="author-leak")  # type: ignore[return-value]

    app = Hinoki()
    app.include(synchronous, asynchronous, wrong)
    with caplog.at_level("ERROR", logger="hinoki.runtime"):
        async with ASGIClient(app) as client:
            for path in ("/auto", "/auto-async", "/wrong"):
                response = await client.get(path)
                assert_internal_error(response.status, response.body)
                assert "leak" not in json.dumps(response.body)

    native_app = Hinoki()

    @http.get("/native")
    async def native() -> AutomaticPostOut:
        return post_row("native-leak")  # type: ignore[return-value]

    native_app.include(native)
    built = Pipeline(native_app)
    with caplog.at_level("ERROR", logger="hinoki.runtime"):
        native_response = await built.handle_native(
            built.bindings[0], "n" * 32, {}, None
        )
    assert_internal_error(native_response.status, json.loads(native_response.body))
    assert b"native-leak" not in native_response.body
    assert any(
        isinstance(cause, ResponseContractError) and "Post" in str(cause)
        for cause in logged_causes(caplog)
    )


async def test_explicit_from_model_works_on_both_lanes() -> None:
    @http.get("/explicit")
    async def explicit() -> AutomaticPostOut:
        return AutomaticPostOut.from_model(post_row("explicit"))

    app = Hinoki()
    app.include(explicit)
    async with ASGIClient(app) as client:
        asgi_response = await client.get("/explicit")
    built = Pipeline(app)
    native_response = await built._call_native(built.bindings[0], {})

    assert (asgi_response.status, asgi_response.body) == (200, {"title": "explicit"})
    assert native_response.status == 200
    assert native_response.body == b'{"title":"explicit"}'


async def test_atomic_model_return_rolls_back_before_response(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'atomic_rollback.sqlite3'}"
    db = await Database.connect(db_url, max_size=1, wait=1)
    try:
        await db.execute(create_table_sql("ds_authors", table_state(Author), "sqlite"))
    finally:
        db.close()

    @derive(Author, only=("id", "name"))
    class AuthorOut(Schema):
        pass

    @http.post("/atomic-model", atomic=True)
    async def create_author_raw() -> AuthorOut:
        return await Author.acreate(name="must-rollback")  # type: ignore[return-value]

    app = Hinoki(include=[create_author_raw], modules=[DatabaseModule(db_url)])
    async with ASGIClient(app) as client:
        response = await client.post("/atomic-model")
        assert_internal_error(response.status, response.body)

    check_db = await Database.connect(db_url, max_size=1, wait=1)
    try:
        with use_database(check_db):
            assert await Author.all().acount() == 0
    finally:
        check_db.close()


async def test_kiso_atomic_model_return_rolls_back_before_response(
    tmp_path: Path,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'kiso_atomic_rollback.sqlite3'}"
    db = await Database.connect(db_url, max_size=1, wait=1)
    try:
        await db.execute(create_table_sql("ds_authors", table_state(Author), "sqlite"))
    finally:
        db.close()

    @derive(Author, only=("id", "name"))
    class AuthorOut(Schema):
        pass

    @http.post("/kiso-atomic-model", atomic=True)
    async def create_author_raw() -> AuthorOut:
        return await Author.acreate(name="kiso-rollback")  # type: ignore[return-value]

    app = Hinoki(include=[create_author_raw], modules=[DatabaseModule(db_url)])
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", worker.address[1])
        writer.write(
            b"POST /kiso-atomic-model HTTP/1.1\r\n"
            b"host: test\r\n"
            b"content-length: 0\r\n\r\n"
        )
        await writer.drain()
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        status = int(raw_head.decode("latin-1").split("\r\n")[0].split()[1])
        writer.close()
        await writer.wait_closed()
        assert status == 500
    finally:
        await worker.stop()

    check_db = await Database.connect(db_url, max_size=1, wait=1)
    try:
        with use_database(check_db):
            assert await Author.all().acount() == 0
    finally:
        check_db.close()


def test_project_response_keeps_schema_narrowing_and_ignores_models() -> None:
    class Extended(AutomaticPostOut):
        label: str = ""

    ext = Extended(title="kept", label="hidden")
    assert project_response(AutomaticPostOut, ext) == {"title": "kept"}
    author = Author(id=uuid4(), name="author")
    assert project_response(AutomaticPostOut, author) is author


async def test_handler_projection_keeps_schema_reply_and_bodyless_responses() -> None:
    schema_value = AutomaticPostOut(title="existing")

    @http.get("/existing")
    def existing() -> AutomaticPostOut:
        return schema_value

    @http.get("/reply")
    def reply() -> AutomaticPostOut:
        return Response(202, b"accepted", [("content-type", "text/plain")])  # type: ignore[return-value]

    @http.get("/empty", status=204)
    def empty() -> AutomaticPostOut:
        return post_row()  # type: ignore[return-value]

    app = Hinoki()
    app.include(existing, reply, empty)
    async with ASGIClient(app) as client:
        existing_response = await client.get("/existing")
        reply_response = await client.get("/reply")
        empty_response = await client.get("/empty")

    assert existing_response.body == {"title": "existing"}
    assert (reply_response.status, reply_response.body) == (202, "accepted")
    assert empty_response.status == 204 and empty_response.body is None


class Account(Model, table="ds_accounts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=20)
    password_hash: str


@derive(Account)
class AccountAll(Schema):
    pass


def test_narrower_selection_rejects_columns_inherited_from_a_derived_base() -> None:
    with pytest.raises(
        RegistrationError,
        match=r"OnlyName: inherited field 'id' from AccountAll is outside only=",
    ):

        @derive(Account, only=("name",))
        class OnlyName(AccountAll):
            pass

    with pytest.raises(
        RegistrationError,
        match=r"NoSecret: inherited field 'password_hash' from AccountAll is in "
        r"exclude=",
    ):

        @derive(Account, exclude=("password_hash",))
        class NoSecret(AccountAll):
            pass


def test_narrower_selection_rejects_model_columns_from_a_hand_written_base() -> None:
    class Identified(Schema):
        id: UUID

    with pytest.raises(RegistrationError, match="inherited field 'id' from Identified"):

        @derive(Account, only=("name",))
        class Named(Identified):
            pass


def test_a_selection_may_keep_or_widen_the_inherited_columns() -> None:
    @derive(Account, only=("name",))
    class NameOnly(Schema):
        pass

    @derive(Account, only=("id", "name"))
    class Wider(NameOnly):
        note: str = ""

    @derive(Account, exclude=("password_hash",))
    class Public(NameOnly):
        pass

    assert set(Wider.specs()) == {"id", "name", "note"}
    assert set(Public.specs()) == {"id", "name"}


def test_derive_rejects_a_non_schema_class_and_a_second_derivation() -> None:
    with pytest.raises(RegistrationError, match="int: @derive needs a Schema"):
        derive(Account)(int)  # type: ignore[type-var]

    with pytest.raises(RegistrationError, match="Twice: already derived"):

        @derive(Account, only=("name",))
        @derive(Account)
        class Twice(Schema):
            pass


def logged_causes(caplog: pytest.LogCaptureFixture) -> list[BaseException]:
    return [
        record.exc_info[1]
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is not None
    ]


def assert_internal_error(status: int, body: dict[str, object]) -> None:
    assert status == 500
    assert body["error"] == "internal_error"
    assert set(body) == {"error", "request_id"}


@derive(Post, only=("title",))
class LimitedPostOut(Schema):
    pass


def test_from_model_rejects_an_invalid_stored_row() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LimitedPostOut.from_model(post_row("x" * 100))
    assert exc_info.value.details[0].code == "max_length"


class LeakyPostOut(AutomaticPostOut):
    internal: str


async def test_a_schema_subclass_is_sent_as_the_declared_schema() -> None:
    value = LeakyPostOut(title="kept", internal="hidden")

    @http.get("/sub")
    async def sub() -> AutomaticPostOut:
        return value

    app = Hinoki()
    app.include(sub)
    async with ASGIClient(app) as client:
        response = await client.get("/sub")
    built = Pipeline(app)
    native = await built._call_native(built.bindings[0], {})

    assert response.body == {"title": "kept"}
    assert native.body == b'{"title":"kept"}'


async def test_a_returned_dict_is_sent_unchanged_when_validation_is_off() -> None:
    @http.get("/raw", validate_response=False)
    def raw() -> AutomaticPostOut:
        return {"title": "t", "extra": 1}  # type: ignore[return-value]

    app = Hinoki()
    app.include(raw)
    async with ASGIClient(app) as client:
        response = await client.get("/raw")

    assert response.body == {"title": "t", "extra": 1}


async def test_lists_and_pages_of_models_fail_closed_without_a_body_leak() -> None:
    @http.get("/many")
    def many() -> list[AutomaticPostOut]:
        return [post_row("leak")]  # type: ignore[list-item]

    @http.get("/paged")
    def paged() -> Page[AutomaticPostOut]:
        return Page(items=[post_row("leak")], page=1, page_size=20, total=1)  # type: ignore[list-item]

    app = Hinoki()
    app.include(many, paged)
    async with ASGIClient(app) as client:
        responses = [await client.get("/many"), await client.get("/paged")]

    for response in responses:
        assert_internal_error(response.status, response.body)
        assert "leak" not in json.dumps(response.body)


async def test_an_annotated_response_schema_rejects_a_model() -> None:
    @http.get("/annotated")
    def annotated() -> Annotated[AutomaticPostOut, "summary"]:
        return post_row("annotated-leak")  # type: ignore[return-value]

    app = Hinoki()
    app.include(annotated)
    async with ASGIClient(app) as client:
        response = await client.get("/annotated")

    assert_internal_error(response.status, response.body)
    assert "annotated-leak" not in json.dumps(response.body)
