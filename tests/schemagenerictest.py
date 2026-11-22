"""Generic schemas: ``class Envelope[T](Schema)`` used as ``Envelope[User]``."""

from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import Hinoki, Schema, computed_field, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.http.request import Request
from hinoki.schema import Field
from hinoki.testing import ASGIClient


class User(Schema):
    name: str


class Envelope[T](Schema):
    data: T
    count: int = 0
    note: T | None = None
    tags: Annotated[list[str], Field(min_length=1)] = Field(
        default_factory=lambda: ["x"]
    )

    @computed_field
    def doubled(self) -> int:
        return self.count * 2


class Pair[A, B](Schema):
    left: A
    right: list[B]
    by_name: dict[str, B] = Field(default_factory=dict)


class Wrapper[T](Schema):
    inner: Envelope[T]


class Plain(Schema):
    value: int


@http.get("/user")
async def show(request: Request) -> Envelope[User]:
    return Envelope[User](data=User(name="Ada"), count=2)


@http.post("/user")
async def echo(request: Request, body: Envelope[User]) -> Envelope[User]:
    return body


def test_a_parameterized_schema_is_a_real_cached_schema() -> None:
    assert Envelope[User] is Envelope[User]
    assert id(Envelope[User]) != id(Envelope[int])
    assert issubclass(Envelope[User], Schema)
    assert Envelope[User].__name__ == "Envelope[User]"


def test_the_parameter_is_substituted_in_fields_and_unions() -> None:
    loaded = Envelope[User].validate({"data": {"name": "Ada"}, "note": {"name": "B"}})
    assert loaded.data == User(name="Ada")
    assert loaded.note == User(name="B")
    assert loaded.doubled == 0
    assert Envelope[int].validate({"data": 3}).data == 3


def test_a_wrong_argument_type_is_reported_on_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        Envelope[int].validate({"data": "x"})
    assert [d.path for d in caught.value.details] == ["data"]


def test_metadata_defaults_factories_and_computed_fields_are_kept() -> None:
    envelope = Envelope[int](data=1, count=4)
    assert envelope.tags == ["x"]
    assert envelope.dump()["doubled"] == 8
    with pytest.raises(ValidationError):
        Envelope[int].validate({"data": 1, "tags": []})


def test_several_parameters_and_containers() -> None:
    pair = Pair[str, int].validate({"left": "a", "right": [1, 2], "by_name": {"k": 3}})
    assert pair.right == [1, 2]
    assert pair.by_name == {"k": 3}
    with pytest.raises(ValidationError):
        Pair[str, int].validate({"left": "a", "right": ["x"]})


def test_a_generic_inside_a_generic() -> None:
    wrapped = Wrapper[User].validate({"inner": {"data": {"name": "Ada"}}})
    assert wrapped.inner.data.name == "Ada"
    assert Wrapper[User].specs()["inner"].ftype.schema is Envelope[User]


def test_the_unparameterized_class_cannot_be_used() -> None:
    with pytest.raises(RegistrationError, match="generic"):
        Envelope.validate({"data": 1})


@pytest.mark.parametrize(
    ("target", "args"),
    [(Envelope, (int, str)), (Pair, (int,)), (Plain, (int,))],
)
def test_the_wrong_number_of_arguments_is_refused(target: Any, args: Any) -> None:
    with pytest.raises(TypeError):
        target[args]


class Tree[T](Schema):
    value: T
    children: "list[Tree[T]]"


def test_a_recursive_generic_is_refused() -> None:
    with pytest.raises(RegistrationError, match="recursive"):
        Tree[int]


async def test_http_and_openapi_use_the_parameterized_schema() -> None:
    app = Hinoki(include=[show, echo])
    document = app.openapi()
    validate(document)
    components = document["components"]["schemas"]
    assert "Envelope_User_" in components
    assert components["Envelope_User_"]["properties"]["data"] == {
        "$ref": "#/components/schemas/User"
    }
    async with ASGIClient(app) as client:
        reply = await client.get("/user")
        good = await client.post("/user", json={"data": {"name": "B"}})
        bad = await client.post("/user", json={"data": 1})
    assert reply.body["data"] == {"name": "Ada"}
    assert reply.body["doubled"] == 4
    assert good.status == 200
    assert bad.status == 422


def test_a_generic_schema_with_an_unresolved_annotation_is_refused() -> None:
    class Dangling[T](Schema):
        data: T
        other: "NotDefinedAnywhere"  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(RegistrationError, match="NotDefinedAnywhere"):
        Dangling[int]
