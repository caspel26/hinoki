"""Wire aliases stay consistent across validation, rendering and OpenAPI."""

from typing import Annotated, Any

import pytest

from hinoki import (
    Field,
    Hinoki,
    Invalid,
    Schema,
    field_check,
    http,
    object_check,
)
from hinoki.errors import RegistrationError, ValidationError
from hinoki.orm import Model, field
from tests.harness import Pool


class Address(Schema):
    postal_code: Annotated[str, Field(alias="postalCode")]


class Person(Schema):
    first_name: Annotated[str, Field(alias="firstName")]
    address: Address
    tags: list[Address]


class Assigned(Schema):
    display_name: str = Field(alias="displayName")
    tags: list[str] = Field(default_factory=list, alias="tagNames")


class Checked(Schema):
    new_password: Annotated[str, Field(alias="newPassword")]
    confirmation: Annotated[str, Field(alias="confirmPassword")]

    @field_check("new_password")
    def require_long_password(self, value: str) -> None:
        if len(value) < 8:
            raise Invalid("min_length", "password is too short")

    @object_check
    def passwords_match(self) -> None:
        if self.new_password != self.confirmation:
            raise Invalid(
                "password_mismatch",
                "passwords differ",
                path="confirmPassword",
            )


@http.post("/people")
def create_person(person: Person) -> Person:
    return person


def test_alias_is_the_wire_name_and_python_name_stays_internal() -> None:
    person = Person.validate(
        {
            "firstName": "Aki",
            "address": {"postalCode": "100-0001"},
            "tags": [{"postalCode": "150-0001"}],
        }
    )
    assert person.first_name == "Aki"
    assert person.dump() == {
        "firstName": "Aki",
        "address": {"postalCode": "100-0001"},
        "tags": [{"postalCode": "150-0001"}],
    }
    assert person.to_dict() == {
        "first_name": "Aki",
        "address": {"postal_code": "100-0001"},
        "tags": [{"postal_code": "150-0001"}],
    }


def test_assigned_alias_is_metadata_and_composes_with_default_factory() -> None:
    value = Assigned.validate({"displayName": "Aki"})
    assert value.display_name == "Aki"
    assert value.tags == []
    assert value.dump() == {"displayName": "Aki", "tagNames": []}
    assert value.to_dict() == {"display_name": "Aki", "tags": []}


def test_aliases_cover_nested_type_errors_and_required_paths() -> None:
    with pytest.raises(ValidationError) as found:
        Person.validate(
            {
                "firstName": 7,
                "address": {"postalCode": 1},
                "tags": [{"postalCode": 2}],
            }
        )
    assert {(item.path, item.code) for item in found.value.details} == {
        ("firstName", "type"),
        ("address.postalCode", "type"),
        ("tags[0].postalCode", "type"),
    }
    with pytest.raises(ValidationError) as missing:
        Person.validate({})
    assert {(item.path, item.code) for item in missing.value.details} == {
        ("firstName", "required"),
        ("address", "required"),
        ("tags", "required"),
    }


def test_alias_only_input_and_duplicate_supply_are_strict() -> None:
    with pytest.raises(ValidationError) as python_name:
        Assigned.validate({"display_name": "Aki"})
    assert {(item.path, item.code) for item in python_name.value.details} == {
        ("displayName", "required"),
        ("display_name", "extra_forbidden"),
    }
    with pytest.raises(ValidationError) as duplicate:
        Assigned.validate({"displayName": "Aki", "display_name": "Ren"})
    assert [(item.path, item.code) for item in duplicate.value.details] == [
        ("displayName", "duplicate_field")
    ]


def test_custom_check_paths_use_wire_names() -> None:
    with pytest.raises(ValidationError) as checked:
        Checked.validate({"newPassword": "short", "confirmPassword": "other"})
    assert {(item.path, item.code) for item in checked.value.details} == {
        ("newPassword", "min_length"),
        ("confirmPassword", "password_mismatch"),
    }


@pytest.mark.parametrize(
    "definition",
    [
        lambda: type(
            "DuplicateAlias",
            (Schema,),
            {
                "__annotations__": {
                    "given_name": Annotated[str, Field(alias="displayName")],
                    "displayName": str,
                }
            },
        ),
        lambda: type(
            "BadAlias",
            (Schema,),
            {"__annotations__": {"name": Annotated[str, Field(alias="nested.name")]}},
        ),
        lambda: type(
            "StackedAlias",
            (Schema,),
            {
                "__annotations__": {
                    "name": Annotated[
                        str,
                        Field(alias="one"),
                        Field(alias="two"),
                    ]
                }
            },
        ),
        lambda: type(
            "EmptyAlias",
            (Schema,),
            {"__annotations__": {"name": Annotated[str, Field(alias="")]}},
        ),
        lambda: type(
            "IndexedAlias",
            (Schema,),
            {"__annotations__": {"name": Annotated[str, Field(alias="items[0]")]}},
        ),
        lambda: type(
            "ControlAlias",
            (Schema,),
            {"__annotations__": {"name": Annotated[str, Field(alias="bad\nname")]}},
        ),
        lambda: type(
            "AssignedConflict",
            (Schema,),
            {
                "__annotations__": {"name": Annotated[str, Field(alias="one")]},
                "name": Field(alias="two"),
            },
        ),
    ],
)
def test_ambiguous_aliases_fail_at_class_creation(definition: Any) -> None:
    with pytest.raises(RegistrationError, match=r"alias|wire name"):
        definition()


def test_identical_alias_metadata_coalesces_and_subclass_can_override() -> None:
    class Base(Schema):
        name: Annotated[str, Field(alias="baseName")] = Field(alias="baseName")

    class Same(Base):
        pass

    class Child(Base):
        name: Annotated[str, Field(alias="childName")]

    assert Same.validate({"baseName": "A"}).dump() == {"baseName": "A"}
    assert Child.validate({"childName": "B"}).dump() == {"childName": "B"}


def test_aliases_do_not_apply_to_model_columns() -> None:
    with pytest.raises(RegistrationError, match="Schema fields, not Model columns"):

        class BadModel(Model, table="alias_bad_models"):
            id: int = field(primary_key=True)
            name: Annotated[str, Field(alias="displayName")]


def test_openapi_uses_aliases_for_properties_and_required_fields() -> None:
    app = Hinoki()
    app.include(create_person)
    component = app.openapi()["components"]["schemas"]["Person"]
    address = app.openapi()["components"]["schemas"]["Address"]
    assert component["required"] == ["firstName", "address", "tags"]
    assert set(component["properties"]) == {"firstName", "address", "tags"}
    assert address["required"] == ["postalCode"]


async def test_http_request_and_json_renderer_use_aliases(pool: Pool) -> None:
    app = Hinoki()
    app.include(create_person)
    client = await pool.start(app)
    response = await client.request(
        "POST",
        "/people",
        body=(
            b'{"firstName":"Aki","address":{"postalCode":"100"},'
            b'"tags":[{"postalCode":"150"}]}'
        ),
        content_type="application/json",
    )
    assert response.status == 200
    assert response.body == {
        "firstName": "Aki",
        "address": {"postalCode": "100"},
        "tags": [{"postalCode": "150"}],
    }
