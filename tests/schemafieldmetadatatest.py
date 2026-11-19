"""``Field(description, examples, deprecated)`` documents a field without
changing how it validates."""

from typing import Annotated, Any, cast

import pytest
from openapi_spec_validator import validate

from hinoki import Hinoki, Schema, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.http.request import Request
from hinoki.schema import Field


class Profile(Schema):
    nick: Annotated[
        str,
        Field(
            max_length=20,
            description="Public handle.",
            examples=("ada", "grace"),
        ),
    ]
    old_nick: Annotated[str | None, Field(deprecated=True)] = None
    age: Annotated[int, Field(ge=0, description="Years.")] = 0


class Holder(Schema):
    profile: Annotated[Profile, Field(description="Who it is.")]


@http.post("/profiles")
async def make(request: Request, data: Holder) -> None: ...


def properties(schema: type[Schema]) -> dict[str, Any]:
    app = Hinoki()
    app.include(make)
    document = app.openapi()
    validate(document)
    return cast(
        dict[str, Any], document["components"]["schemas"][schema.__name__]["properties"]
    )


def test_description_examples_and_deprecated_reach_the_document() -> None:
    props = properties(Profile)
    assert props["nick"]["description"] == "Public handle."
    assert props["nick"]["examples"] == ["ada", "grace"]
    assert props["nick"]["maxLength"] == 20
    assert props["old_nick"]["deprecated"] is True
    assert props["old_nick"]["anyOf"][1] == {"type": "null"}
    assert "deprecated" not in props["age"]


def test_a_nested_schema_keeps_its_reference_and_gains_a_description() -> None:
    holder = properties(Holder)["profile"]
    assert holder["description"] == "Who it is."
    assert holder["$ref"].endswith("/Profile")


def test_a_nullable_field_describes_the_whole_union() -> None:
    props = properties(Profile)
    assert "description" not in props["old_nick"]["anyOf"][0]


def test_documentation_does_not_change_validation() -> None:
    assert Profile(nick="ada").nick == "ada"
    with pytest.raises(ValidationError):
        Profile.validate({"nick": "x" * 21})
    with pytest.raises(ValidationError):
        Profile.validate({"nick": "ada", "age": -1})


def test_the_notes_survive_alongside_a_wire_alias() -> None:
    class Aliased(Schema):
        given_name: Annotated[str, Field(alias="givenName", description="First name.")]

    assert Aliased.validate({"givenName": "Ada"}).given_name == "Ada"
    app = Hinoki()

    @http.post("/aliased")
    async def aliased(request: Request, data: Aliased) -> None: ...

    app.include(aliased)
    schema = app.openapi()["components"]["schemas"]["Aliased"]
    assert schema["properties"]["givenName"]["description"] == "First name."


def declare(annotation: Any) -> None:
    class Bad(Schema):
        name: annotation

    Bad.fields()


def test_a_bad_description_is_refused_at_registration() -> None:
    with pytest.raises(RegistrationError, match="description"):
        declare(Annotated[str, Field(description="")])


def test_examples_must_be_a_non_empty_sequence() -> None:
    with pytest.raises(RegistrationError, match="examples"):
        declare(Annotated[str, Field(examples=())])


def test_the_innermost_declaration_wins_when_annotations_nest() -> None:
    Inner = Annotated[str, Field(description="inner")]

    class Outer(Schema):
        name: Annotated[Inner, Field(description="outer")]

    assert Outer.fields()["name"].ftype.info.description == "inner"
