"""``@before_validate`` reshapes raw wire input before structural validation."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from hinoki import Field, Hinoki, Invalid, Schema, before_validate, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.testing import ASGIClient


class Signup(Schema):
    email: str
    age: int

    @before_validate
    @classmethod
    def lowercase_email(cls, data: dict[str, Any]) -> dict[str, Any]:
        email = data.get("email")
        if isinstance(email, str):
            return {**data, "email": email.strip().lower()}
        return data


class Wrapped(Schema):
    value: int

    @before_validate
    @staticmethod
    def unwrap(data: dict[str, Any]) -> dict[str, Any]:
        inner = data.get("wrapper")
        return {"value": inner} if inner is not None else data


class Cart(Schema):
    owner: Signup
    items: list[Wrapped]
    by_name: dict[str, Wrapped]
    maybe: Wrapped | None = None


class Strict(Schema):
    value: int

    @before_validate
    @classmethod
    def refuse_negative(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data.get("value"), int) and data["value"] < 0:
            raise Invalid("negative", "value must not be negative", path="value")
        return data


class Holder(Schema):
    inner: Strict


class Aliased(Schema):
    user_name: Annotated[str, Field(alias="userName")]

    @before_validate
    @classmethod
    def rename(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "name" in data:
            return {"userName": data["name"]}
        return data


class Dog(Schema):
    kind: Annotated[str, Field(one_of=("dog",))]
    bark: int

    @before_validate
    @classmethod
    def default_bark(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {"bark": 1, **data}


class Cat(Schema):
    kind: Annotated[str, Field(one_of=("cat",))]


class Pets(Schema):
    pet: Annotated[Dog | Cat, Field(discriminator="kind")]


class NoPath(Schema):
    value: int

    @before_validate
    @classmethod
    def refuse(cls, _data: dict[str, Any]) -> dict[str, Any]:
        raise Invalid("nope", "refused")


class Outer(Schema):
    rows: list[NoPath]


class Node(Schema):
    child: Node | None = None

    @before_validate
    @classmethod
    def keep(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data


class Renamed(Schema):
    n: int

    @before_validate
    @classmethod
    def rename(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {"n": data["m"]} if "m" in data else data


class Fragile(Schema):
    v: int

    @before_validate
    @classmethod
    def reach(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {"v": data["missing"]}


class Tree(Schema):
    kids: list[Tree] | None = None

    @before_validate
    @classmethod
    def keep(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data


class Base(Schema):
    n: int

    @before_validate
    @classmethod
    def double(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "n": data["n"] * 2}


class Child(Base):
    @before_validate
    @classmethod
    def double(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "n": data["n"] * 3}


def test_root_hook_runs_before_validation() -> None:
    parsed = Signup.validate({"email": "  A@B.IO ", "age": 3})
    assert parsed.email == "a@b.io"


def test_hook_does_not_mutate_callers_input() -> None:
    raw = {"email": "A@B.IO", "age": 3}
    Signup.validate(raw)
    assert raw["email"] == "A@B.IO"


def test_static_hook_and_nested_containers() -> None:
    parsed = Cart.validate(
        {
            "owner": {"email": "X@Y.Z", "age": 1},
            "items": [{"wrapper": 1}, {"value": 2}],
            "by_name": {"a": {"wrapper": 5}},
            "maybe": {"wrapper": 9},
        }
    )
    assert parsed.owner.email == "x@y.z"
    assert [item.value for item in parsed.items] == [1, 2]
    assert parsed.by_name["a"].value == 5
    assert parsed.maybe is not None and parsed.maybe.value == 9


def test_null_optional_nested_is_left_alone() -> None:
    parsed = Cart.validate(
        {
            "owner": {"email": "x@y.z", "age": 1},
            "items": [],
            "by_name": {},
            "maybe": None,
        }
    )
    assert parsed.maybe is None


def test_invalid_becomes_validation_error_with_full_path() -> None:
    with pytest.raises(ValidationError) as caught:
        Holder.validate({"inner": {"value": -1}})
    detail = caught.value.details[0]
    assert (detail.path, detail.code) == ("inner.value", "negative")


def test_invalid_without_path_uses_object_path() -> None:
    with pytest.raises(ValidationError) as caught:
        Outer.validate({"rows": [{"value": 1}, {"value": 2}]})
    assert caught.value.details[0].path == "rows.0"


def test_non_mapping_input_is_left_for_structural_validation() -> None:
    with pytest.raises(ValidationError):
        Signup.validate(["not", "a", "mapping"])
    with pytest.raises(ValidationError):
        Holder.validate({"inner": 5})


def test_hook_sees_wire_names() -> None:
    assert Aliased.validate({"name": "ada"}).user_name == "ada"


def test_discriminated_variant_hook_runs_for_selected_variant_only() -> None:
    parsed = Pets.validate({"pet": {"kind": "dog"}})
    assert isinstance(parsed.pet, Dog) and parsed.pet.bark == 1
    assert isinstance(Pets.validate({"pet": {"kind": "cat"}}).pet, Cat)


def test_subclass_override_replaces_inherited_hook() -> None:
    assert Base.validate({"n": 1}).n == 2
    assert Child.validate({"n": 1}).n == 3


def test_hook_must_return_a_mapping() -> None:
    class Bad(Schema):
        value: int

        @before_validate
        @classmethod
        def broken(cls, _data: dict[str, Any]) -> dict[str, Any]:
            return None  # type: ignore[return-value]

    with pytest.raises(TypeError, match="mapping"):
        Bad.validate({"value": 1})


def test_must_wrap_classmethod_or_staticmethod() -> None:
    def build() -> None:
        class Plain(Schema):
            value: int

            @before_validate
            def hook(self, data: dict[str, Any]) -> dict[str, Any]:
                return data

    with pytest.raises(RegistrationError, match="classmethod or staticmethod"):
        build()


def test_hook_must_be_synchronous() -> None:
    def build() -> None:
        class Slow(Schema):
            value: int

            @before_validate
            @classmethod
            async def hook(cls, data: dict[str, Any]) -> dict[str, Any]:
                return data

    with pytest.raises(RegistrationError, match="synchronous"):
        build()


def test_plain_union_holding_hooked_schemas_is_refused_at_definition() -> None:
    def build() -> None:
        class Either(Schema):
            pick: list[Wrapped] | int

    with pytest.raises(RegistrationError, match="discriminator"):
        build()


def test_unexpected_input_shape_in_hook_is_a_validation_error() -> None:
    with pytest.raises(ValidationError) as caught:
        Fragile.validate({"v": 1})
    assert caught.value.details[0].code == "invalid_input"


def test_recursive_nesting_within_the_schema_limit_validates() -> None:
    raw: dict[str, Any] = {}
    for _ in range(50):
        raw = {"kids": [raw]}
    assert Tree.validate(raw).kids is not None


def test_depth_is_bounded() -> None:
    raw: dict[str, Any] = {}
    for _ in range(200):
        raw = {"child": raw}
    with pytest.raises(ValidationError) as caught:
        Node.validate(raw)
    assert caught.value.details[0].code == "too_deep"


def test_validate_text_runs_hook_before_coercion() -> None:
    assert Renamed.validate_text({"m": ["4"]}).n == 4


@pytest.mark.asyncio
async def test_request_body_goes_through_hook() -> None:
    app = Hinoki(title="before")

    @http.post("/signup")
    async def signup(body: Signup) -> dict[str, str]:
        return {"email": body.email}

    app.include(signup)
    async with ASGIClient(app) as client:
        response = await client.post("/signup", json={"email": "A@B.C", "age": 1})
    assert response.status == 200
    assert response.body == {"email": "a@b.c"}
