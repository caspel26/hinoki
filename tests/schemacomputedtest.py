"""``@computed_field`` adds a read-only value to the output of a schema."""

from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import Hinoki, Schema, http
from hinoki.computed import computed_field
from hinoki.errors import RegistrationError, ValidationError
from hinoki.http.request import Request
from hinoki.schema import Field
from hinoki.testing import ASGIClient


class Person(Schema):
    first: str
    last: str
    nick: str = ""

    @computed_field
    def full_name(self) -> str:
        return f"{self.first} {self.last}"

    @computed_field(alias="initialLetters")
    def initials(self) -> list[str]:
        return [self.first[0], self.last[0]]


class Team(Schema):
    lead: Person
    size: int = 1

    @computed_field
    def label(self) -> str:
        return f"{self.lead.full_name} +{self.size - 1}"


class Child(Person):
    extra: int = 0

    @computed_field
    def doubled(self) -> int:
        return self.extra * 2


@http.get("/person")
async def show(request: Request) -> Person:
    return Person(first="Ada", last="Lovelace")


@http.post("/person")
async def make(request: Request, data: Person) -> Person:
    return data


@http.get("/team")
async def team(request: Request) -> Team:
    return Team(lead=Person(first="Grace", last="Hopper"), size=3)


def test_a_computed_value_reads_like_an_attribute() -> None:
    person = Person(first="Ada", last="Lovelace")
    assert person.full_name == "Ada Lovelace"
    assert person.initials == ["A", "L"]


def test_dump_adds_computed_values_after_the_fields() -> None:
    person = Person(first="Ada", last="Lovelace")
    assert person.dump() == {
        "first": "Ada",
        "last": "Lovelace",
        "nick": "",
        "full_name": "Ada Lovelace",
        "initialLetters": ["A", "L"],
    }
    assert list(person.dump())[-2:] == ["full_name", "initialLetters"]


def test_by_alias_false_uses_the_python_name() -> None:
    dumped = Person(first="A", last="B").dump(by_alias=False)
    assert dumped["initials"] == ["A", "B"]
    assert "initialLetters" not in dumped


def test_projection_can_select_and_drop_computed_values() -> None:
    person = Person(first="Ada", last="Lovelace")
    assert person.dump(include={"full_name"}) == {"full_name": "Ada Lovelace"}
    assert "full_name" not in person.dump(exclude={"full_name"})
    assert person.dump(include={"first", "initials"}, by_alias=False) == {
        "first": "Ada",
        "initials": ["A", "L"],
    }


def test_exclude_unset_keeps_computed_values() -> None:
    person = Person(first="Ada", last="Lovelace")
    assert person.dump(exclude_unset=True) == {
        "first": "Ada",
        "last": "Lovelace",
        "full_name": "Ada Lovelace",
        "initialLetters": ["A", "L"],
    }


def test_to_dict_stays_the_supplied_input() -> None:
    assert Person(first="Ada", last="L").to_dict() == {
        "first": "Ada",
        "last": "L",
        "nick": "",
    }


def test_nested_schemas_dump_their_computed_values() -> None:
    dumped = Team(lead=Person(first="Grace", last="Hopper"), size=3).dump()
    assert dumped["lead"]["full_name"] == "Grace Hopper"
    assert dumped["label"] == "Grace Hopper +2"


def test_a_subclass_inherits_and_adds_computed_values() -> None:
    child = Child(first="A", last="B", extra=4)
    assert list(child.dump())[-3:] == ["full_name", "initialLetters", "doubled"]
    assert child.dump()["doubled"] == 8
    assert "doubled" not in Person(first="A", last="B").dump()


def test_a_computed_value_is_never_accepted_as_input() -> None:
    with pytest.raises(ValidationError) as caught:
        Person.validate({"first": "A", "last": "B", "full_name": "X"})
    assert [d.code for d in caught.value.details] == ["extra_forbidden"]


async def test_a_response_includes_computed_values() -> None:
    async with ASGIClient(Hinoki(include=[show, make, team])) as client:
        reply = await client.get("/person")
        echoed = await client.post("/person", json={"first": "A", "last": "B"})
        nested = await client.get("/team")
    assert reply.body["full_name"] == "Ada Lovelace"
    assert reply.body["initialLetters"] == ["A", "L"]
    assert echoed.body["full_name"] == "A B"
    assert nested.body["lead"]["full_name"] == "Grace Hopper"
    assert nested.body["label"] == "Grace Hopper +2"


async def test_a_client_cannot_send_a_computed_value() -> None:
    async with ASGIClient(Hinoki(include=[make])) as client:
        reply = await client.post(
            "/person", json={"first": "A", "last": "B", "full_name": "X"}
        )
    assert reply.status == 422


def document() -> dict[str, Any]:
    app = Hinoki(include=[show, make, team])
    result = app.openapi()
    validate(result)
    schemas: dict[str, Any] = result["components"]["schemas"]
    return schemas


def test_openapi_describes_computed_values_as_read_only() -> None:
    props = document()["Person"]["properties"]
    assert props["full_name"] == {"type": "string", "readOnly": True}
    assert props["initialLetters"] == {
        "type": "array",
        "items": {"type": "string"},
        "readOnly": True,
    }
    assert "full_name" not in document()["Person"]["required"]
    assert "initialLetters" not in document()["Person"]["required"]


def test_openapi_describes_the_documented_notes() -> None:
    class Noted(Schema):
        a: int

        @computed_field(description="Twice a.", examples=(2,))
        def twice(self) -> int:
            return self.a * 2

    @http.get("/noted")
    async def noted(request: Request) -> Noted:
        return Noted(a=1)

    props = Hinoki(include=[noted]).openapi()["components"]["schemas"]["Noted"][
        "properties"
    ]
    assert props["twice"] == {
        "type": "integer",
        "readOnly": True,
        "description": "Twice a.",
        "examples": [2],
    }


def test_computed_fields_are_listed_by_the_class() -> None:
    names = {name: spec.wire_name for name, spec in Person.computed_fields().items()}
    assert names == {"full_name": "full_name", "initials": "initialLetters"}
    assert Person.fields().keys() == {"first", "last", "nick"}


def declare(namespace: dict[str, Any]) -> None:
    type("Bad", (Schema,), {"__annotations__": {"a": int}, **namespace})


def test_a_computed_field_needs_a_return_annotation() -> None:
    def bare(self: Any):  # type: ignore[no-untyped-def]
        return 1

    with pytest.raises(RegistrationError, match="return annotation"):
        declare({"bare": computed_field(bare)})


def test_a_computed_field_cannot_share_a_field_name() -> None:
    def a(self: Any) -> int:
        return 1

    with pytest.raises(RegistrationError, match="already a field"):
        declare({"a": computed_field(a)})


def test_a_computed_field_cannot_take_arguments() -> None:
    def other(self: Any, more: int) -> int:
        return more

    with pytest.raises(RegistrationError, match="no arguments"):
        declare({"other": computed_field(other)})  # type: ignore[arg-type]


def test_two_computed_fields_cannot_share_a_wire_name() -> None:
    def one(self: Any) -> int:
        return 1

    def two(self: Any) -> int:
        return 2

    with pytest.raises(RegistrationError, match="wire name"):
        declare(
            {
                "one": computed_field(alias="x")(one),
                "two": computed_field(alias="x")(two),
            }
        )


def test_a_computed_field_cannot_use_a_field_wire_name() -> None:
    class Aliased(Schema):
        given: Annotated[str, Field(alias="n")]

    def other(self: Any) -> int:
        return 1

    with pytest.raises(RegistrationError, match="wire name"):
        type(
            "Bad",
            (Aliased,),
            {"other": computed_field(alias="n")(other)},
        )
