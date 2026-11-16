"""Schema string normalizers transform validated input before check hooks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Annotated, cast

import pytest

from hinoki import (
    UNSET,
    Field,
    Hinoki,
    Invalid,
    Schema,
    Unset,
    field_check,
    field_normalize,
    http,
    object_check,
)
from hinoki.errors import RegistrationError, ValidationError
from hinoki.testing import ASGIClient

normalizer_calls: list[tuple[str, str]] = []
check_calls: list[tuple[str, str]] = []
endpoint_calls: list[str] = []


class Signup(Schema):
    username: Annotated[str, Field(alias="userName", min_length=3, max_length=12)]
    email: str | None = None
    maybe: str | Unset = UNSET

    @field_normalize("username")
    def trim_username(self, value: str) -> str:
        normalizer_calls.append(("username", value))
        return value.strip()

    @field_normalize("email")
    def lowercase_email(self, value: str) -> str:
        normalizer_calls.append(("email", value))
        return value.lower()

    @field_normalize("maybe")
    def normalize_maybe(self, value: str) -> str:
        normalizer_calls.append(("maybe", value))
        return value.strip()

    @field_check("username")
    def reject_reserved(self, value: str) -> None:
        check_calls.append(("username", value))
        if value == "admin":
            raise Invalid("reserved", "choose another username")

    @object_check
    def record_final_values(self) -> None:
        check_calls.append(("object", self.username))


class Envelope(Schema):
    signup: Signup
    count: int = 0


model_normalizer_calls: list[str] = []


class ModelChild(Schema):
    value: Annotated[str, Field(min_length=2)]

    @field_normalize("value")
    def trim(self, value: str) -> str:
        model_normalizer_calls.append(value)
        return value.strip()


class ModelEnvelope(Schema):
    child: ModelChild
    count: int


recursive_normalizer_calls: list[str] = []


class RecursiveLeaf(Schema):
    value: str

    @field_normalize("value")
    def trim(self, value: str) -> str:
        recursive_normalizer_calls.append(value)
        return value.strip()

    @field_check("value")
    def reject_bad(self, value: str) -> None:
        if value == "bad":
            raise Invalid("reserved", "bad value")


class RecursiveA(Schema):
    b: RecursiveB | None = None
    leaf: RecursiveLeaf


class RecursiveB(Schema):
    a: RecursiveA | None = None
    count: int


class FakeModelRow:
    __table__ = object()

    def __init__(self, value: str) -> None:
        self.value = value


class FakeRecursiveARow:
    __table__ = object()

    def __init__(self, leaf: FakeModelRow) -> None:
        self.leaf = leaf
        self.b = None


class FakeRecursiveBRow:
    __table__ = object()

    def __init__(self, a: FakeRecursiveARow) -> None:
        self.a = a
        self.count = 1


@http.post("/signup-sync")
def signup_sync(data: Signup) -> dict[str, str | None]:
    endpoint_calls.append("sync")
    return {"username": data.username, "email": data.email}


@http.post("/signup-async")
async def signup_async(data: Signup) -> dict[str, str | None]:
    endpoint_calls.append("async")
    return {"username": data.username, "email": data.email}


def test_manual_wire_and_model_construction_share_normalized_values() -> None:
    normalizer_calls.clear()
    check_calls.clear()
    manual = Signup(username=" Aki ", email="A@EXAMPLE.COM")
    assert (manual.username, manual.email, manual.maybe) == (
        "Aki",
        "a@example.com",
        UNSET,
    )
    assert manual.dump() == {"userName": "Aki", "email": "a@example.com"}
    assert normalizer_calls == [("username", " Aki "), ("email", "A@EXAMPLE.COM")]
    assert check_calls == [("username", "Aki"), ("object", "Aki")]

    with pytest.raises(ValidationError) as rejected:
        Signup.validate({"userName": " admin "})
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("userName", "reserved")
    ]

    normalizer_calls.clear()
    check_calls.clear()
    parsed = Signup.validate({"userName": " Aki ", "email": "A@EXAMPLE.COM"})
    assert (parsed.username, parsed.email) == ("Aki", "a@example.com")
    assert normalizer_calls == [("username", " Aki "), ("email", "A@EXAMPLE.COM")]
    assert check_calls == [("username", "Aki"), ("object", "Aki")]

    normalizer_calls.clear()
    from_row = Signup.from_model(
        SimpleNamespace(username=" Aki ", email="A@EXAMPLE.COM", maybe="  ok  ")
    )
    assert (from_row.username, from_row.email, from_row.maybe) == (
        "Aki",
        "a@example.com",
        "ok",
    )
    assert normalizer_calls == [
        ("username", " Aki "),
        ("email", "A@EXAMPLE.COM"),
        ("maybe", "  ok  "),
    ]

    class Defaulted(Schema):
        value: str = " default "

        @field_normalize("value")
        def trim(self, value: str) -> str:
            return value.strip()

    defaulted = Defaulted()
    assert defaulted.value == "default"
    assert defaulted.dump(exclude_unset=True) == {}


def test_normalizers_run_once_on_new_nested_instances() -> None:
    normalizer_calls.clear()
    child = Signup(username=" Aki ")
    assert normalizer_calls == [("username", " Aki ")]
    parent = Envelope(signup=child)
    assert parent.signup is child
    assert normalizer_calls == [("username", " Aki ")]

    normalizer_calls.clear()
    parsed = Envelope.validate({"signup": {"userName": " Aki "}})
    assert parsed.signup.username == "Aki"
    assert normalizer_calls == [("username", " Aki ")]

    normalizer_calls.clear()
    with pytest.raises(ValidationError):
        Envelope.validate({"signup": {"userName": " Aki "}, "count": "bad"})
    assert normalizer_calls == []

    with pytest.raises(ValidationError) as nested_invalid:
        Envelope.validate({"signup": {"userName": "   "}})
    assert nested_invalid.value.details[0].path == "signup.userName"


def test_initial_structural_failure_skips_hooks_and_normalized_value_revalidates() -> (
    None
):
    normalizer_calls.clear()
    with pytest.raises(ValidationError) as before_normalization:
        Signup.validate({"userName": "x"})
    assert before_normalization.value.details[0].path == "userName"
    assert before_normalization.value.details[0].code == "min_length"
    assert normalizer_calls == []

    normalizer_calls.clear()
    with pytest.raises(ValidationError) as after_normalization:
        Signup.validate({"userName": "   ", "email": "invalid"})
    assert [(item.path, item.code) for item in after_normalization.value.details] == [
        ("userName", "min_length")
    ]
    assert normalizer_calls == [("username", "   "), ("email", "invalid")]

    with pytest.raises(ValidationError) as direct:
        Signup(username="   ")
    assert [(item.path, item.code) for item in direct.value.details] == [
        ("username", "min_length")
    ]

    with pytest.raises(ValidationError) as raw_too_long:
        Signup.validate({"userName": " " * 13})
    assert raw_too_long.value.details[0].code == "max_length"


def test_post_init_runs_before_normalization_and_cannot_be_rescued() -> None:
    calls: list[str] = []

    class PostInit(Schema):
        value: str

        def __post_init__(self) -> None:
            object.__setattr__(self, "value", self.value.strip())

        @field_normalize("value")
        def uppercase(self, value: str) -> str:
            calls.append(value)
            return value.upper()

    assert PostInit(value=" before ").value == "BEFORE"
    assert calls == ["before"]
    assert PostInit.from_model(SimpleNamespace(value=" from row ")).value == "FROM ROW"

    class InvalidPostInit(Schema):
        value: str

        def __post_init__(self) -> None:
            object.__setattr__(self, "value", 3)

        @field_normalize("value")
        def should_not_run(self, value: str) -> str:
            calls.append(value)
            return value

    with pytest.raises(ValidationError):
        InvalidPostInit(value="ok")
    assert calls == ["before", "from row"]


def test_from_model_defers_nested_normalizers_until_parent_validation() -> None:
    model_normalizer_calls.clear()
    with pytest.raises(ValidationError):
        ModelEnvelope.from_model(
            SimpleNamespace(child=FakeModelRow(" raw "), count="bad")
        )
    assert model_normalizer_calls == []

    parent = ModelEnvelope.from_model(
        SimpleNamespace(child=FakeModelRow(" raw "), count=1)
    )
    assert parent.child.value == "raw"
    assert model_normalizer_calls == [" raw "]

    with pytest.raises(ValidationError) as normalized:
        ModelEnvelope.from_model(SimpleNamespace(child=FakeModelRow("  "), count=1))
    assert normalized.value.details[0].path == "child.value"


def test_recursive_schema_graph_does_not_run_normalizers_before_validation() -> None:
    recursive_normalizer_calls.clear()
    with pytest.raises(ValidationError):
        RecursiveB.validate(
            {
                "a": {"leaf": {"value": " raw "}},
                "count": "invalid",
            }
        )
    assert recursive_normalizer_calls == []


def test_recursive_schema_graph_runs_deferred_model_checks() -> None:
    RecursiveA.validate({"leaf": {"value": "valid"}})
    row = FakeRecursiveBRow(FakeRecursiveARow(FakeModelRow(" bad ")))
    with pytest.raises(ValidationError) as rejected:
        RecursiveB.from_model(row)
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("a.leaf.value", "reserved")
    ]


def test_custom_initializer_uses_the_same_normalization_step() -> None:
    class CustomInit(Schema):
        value: str

        def __init__(self, *, value: str) -> None:
            object.__setattr__(self, "value", value)

        @field_normalize("value")
        def trim(self, value: str) -> str:
            return value.strip()

    assert CustomInit(value=" custom ").value == "custom"
    assert CustomInit.from_model(SimpleNamespace(value=" row ")).value == "row"


def test_normalized_outputs_are_staged_and_checks_see_only_final_values() -> None:
    normalized_instances: list[object] = []
    proposals: list[str] = []

    class Staged(Schema):
        first: Annotated[str, Field(min_length=2)]
        second: Annotated[str, Field(min_length=2)]

        def __post_init__(self) -> None:
            normalized_instances.append(self)

        @field_normalize("first")
        def trim_first(self, value: str) -> str:
            proposals.append(value)
            return value.strip()

        @field_normalize("second")
        def invalid_second(self, value: str) -> str:
            proposals.append(value)
            return "x"

        @object_check
        def should_not_run(self) -> None:
            raise AssertionError("checks must wait until every output validates")

    with pytest.raises(ValidationError):
        Staged(first=" first ", second=" second ")
    assert proposals == [" first ", " second "]
    assert normalized_instances[0].first == " first "  # type: ignore[attr-defined]
    assert normalized_instances[0].second == " second "  # type: ignore[attr-defined]

    instances: list[Schema] = []

    class NestedStage(Schema):
        value: Annotated[str, Field(min_length=2)]

        def __post_init__(self) -> None:
            instances.append(self)

        @field_normalize("value")
        def trim(self, value: str) -> str:
            return value.strip()

    class RootStage(Schema):
        child: NestedStage
        value: Annotated[str, Field(min_length=2)]

        def __post_init__(self) -> None:
            instances.append(self)

        @field_normalize("value")
        def invalid(self, value: str) -> str:
            return "x"

    RootStage.__annotations__["child"] = NestedStage

    with pytest.raises(ValidationError):
        RootStage.validate({"child": {"value": " nested "}, "value": " parent "})
    assert cast(NestedStage, instances[0]).value == " nested "
    assert cast(RootStage, instances[1]).value == " parent "


def test_checks_must_return_none_and_async_checks_fail_at_registration() -> None:
    class ReturningCheck(Schema):
        value: str

        @field_check("value")
        def mistaken_transform(self, value: str) -> str:
            return value.strip()

    with pytest.raises(TypeError, match="must return None"):
        ReturningCheck(value="value")

    def declare_async_check() -> None:
        class AsyncCheck(Schema):
            value: str

            @field_check("value")
            async def reject(self, value: str) -> None:
                return None

    with pytest.raises(RegistrationError, match="must be synchronous"):
        declare_async_check()


def test_static_check_markers_are_preserved_and_static_normalizers_are_rejected() -> (
    None
):
    class StaticCheck(Schema):
        value: str

        @field_check("value")
        @staticmethod
        def reject(_value: str) -> None:
            raise Invalid("static", "static check ran")

    with pytest.raises(ValidationError) as rejected:
        StaticCheck(value="bad")
    assert rejected.value.details[0].code == "static"

    def declare_static_normalizer() -> None:
        class StaticNormalizer(Schema):
            value: str

            @field_normalize("value")
            @staticmethod
            def trim(_self: object, value: str) -> str:
                return value.strip()

    with pytest.raises(RegistrationError, match="instance methods"):
        declare_static_normalizer()


def test_awaitable_hook_results_are_closed_and_reported_as_programming_errors() -> None:
    async def result(value: str) -> str:
        return value

    class AwaitableNormalizer(Schema):
        value: str

        @field_normalize("value")
        def mistaken(self, value: str) -> str:
            return result(value)  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must be synchronous"):
        AwaitableNormalizer(value="value")

    class AwaitableCheck(Schema):
        value: str

        @field_check("value")
        def mistaken(self, value: str) -> None:
            return asyncio.sleep(0)  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must return None"):
        AwaitableCheck(value="value")


def test_normalizer_registration_rejects_bad_annotations_and_types() -> None:
    def declare_wrong_type() -> None:
        class WrongType(Schema):
            count: int

            @field_normalize("count")
            def canonicalize(self, value: str) -> str:
                return value

    def declare_bad_signature() -> None:
        class BadSignature(Schema):
            value: str

            @field_normalize("value")
            def canonicalize(self, value: str, extra: str) -> str:
                return value + extra

    def declare_bad_annotation() -> None:
        class BadAnnotation(Schema):
            value: str

            @field_normalize("value")  # type: ignore[type-var]
            def canonicalize(self, value: int) -> int:
                return value

    for declare in (declare_wrong_type, declare_bad_signature, declare_bad_annotation):
        with pytest.raises(RegistrationError):
            declare()


def test_normalizer_registration_rejects_unknown_and_aliased_targets() -> None:
    def declare_unknown_target() -> None:
        class UnknownTarget(Schema):
            value: str

            @field_normalize("missing")
            def canonicalize(self, value: str) -> str:
                return value

    def declare_alias_target() -> None:
        class AliasTarget(Schema):
            value: Annotated[str, Field(alias="wireValue")]

            @field_normalize("wireValue")
            def canonicalize(self, value: str) -> str:
                return value

    for declare in (declare_unknown_target, declare_alias_target):
        with pytest.raises(RegistrationError):
            declare()


def test_normalizer_registration_rejects_async_and_duplicate_hooks() -> None:
    def declare_async_normalizer() -> None:
        class AsyncNormalizer(Schema):
            value: str

            @field_normalize("value")  # type: ignore[type-var]
            async def canonicalize(self, value: str) -> str:
                return value

    def declare_duplicate_target() -> None:
        class DuplicateTarget(Schema):
            value: str

            @field_normalize("value")
            def first(self, value: str) -> str:
                return value

            @field_normalize("value")
            def second(self, value: str) -> str:
                return value

    for declare in (
        declare_async_normalizer,
        declare_duplicate_target,
    ):
        with pytest.raises(RegistrationError):
            declare()


def test_inheritance_can_replace_or_remove_a_normalizer() -> None:
    class Base(Schema):
        value: str

        @field_normalize("value")
        def canonicalize(self, value: str) -> str:
            return value.strip()

    class Replaced(Base):
        @field_normalize("value")
        def canonicalize(self, value: str) -> str:
            return value.upper()

    class Removed(Base):
        def canonicalize(self, value: str) -> str:
            return value

    assert Replaced(value=" aki ").value == " AKI "
    assert Removed(value=" aki ").value == " aki "


@pytest.mark.asyncio
async def test_sync_and_async_endpoints_receive_the_same_normalized_request() -> None:
    app = Hinoki()
    app.include(signup_sync, signup_async)
    endpoint_calls.clear()
    async with ASGIClient(app) as client:
        for path in ("/signup-sync", "/signup-async"):
            response = await client.post(
                path, json={"userName": " Aki ", "email": "A@EXAMPLE.COM"}
            )
            assert response.status == 200
            assert response.body == {"username": "Aki", "email": "a@example.com"}
        assert endpoint_calls == ["sync", "async"]

        endpoint_calls.clear()
        normalizer_calls.clear()
        for path in ("/signup-sync", "/signup-async"):
            response = await client.post(path, json={"userName": "   "})
            assert response.status == 422
            assert response.body["details"][0]["path"] == "data.userName"
            assert endpoint_calls == []
        assert normalizer_calls == [("username", "   "), ("username", "   ")]
