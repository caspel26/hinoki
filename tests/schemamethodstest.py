"""Schema.to_dict and Schema.from_model."""

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from hinoki import UNSET, Schema, Unset
from hinoki.errors import RelationNotLoaded
from hinoki.orm import Model, field, foreign_key
from hinoki.relations import Related


class Address(Schema):
    city: str
    zip_code: str | None = None


class Person(Schema):
    id: UUID
    name: str
    born: datetime
    address: Address
    tags: list[str]
    homes: list[Address]


class Patch(Schema):
    name: str | Unset = UNSET
    nick: str | None | Unset = UNSET


def test_to_dict_keeps_python_values_and_omits_unset() -> None:
    key, born = uuid4(), datetime(2026, 1, 2, tzinfo=UTC)
    person = Person(
        id=key,
        name="Ada",
        born=born,
        address=Address(city="Rome"),
        tags=["a"],
        homes=[Address(city="X", zip_code="1")],
    )
    data = person.to_dict()
    assert data["id"] is key and data["born"] is born  # not JSON strings
    assert data["address"] == {"city": "Rome", "zip_code": None}
    assert data["homes"] == [{"city": "X", "zip_code": "1"}]
    assert person.dump()["id"] == str(key)  # dump() is unchanged
    assert set(person.to_dict(exclude={"homes", "tags"})) == {
        "id",
        "name",
        "born",
        "address",
    }


def test_to_dict_is_exactly_what_was_sent_for_a_patch() -> None:
    assert Patch().to_dict() == {}
    assert Patch(name="x").to_dict() == {"name": "x"}
    assert Patch(nick=None).to_dict() == {"nick": None}  # None is a value, UNSET is not


class Owner(Model, table="sm_owners"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    pets: ClassVar["Related[list[Pet]]"]


class Pet(Model, table="sm_pets"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Owner, on_delete="cascade", related_name="pets")
    title: str
    owner: ClassVar[Related[Owner]]


class OwnerOut(Schema):
    id: UUID
    name: str


class PetOut(Schema):
    id: UUID
    title: str
    owner: OwnerOut
    shout: str


def test_from_model_copies_fields_and_takes_extras() -> None:
    owner = Owner(name="Rae")
    out = OwnerOut.from_model(owner)
    assert (out.id, out.name) == (owner.id, "Rae")
    assert OwnerOut.from_model(owner, name="override").name == "override"


def test_from_model_converts_nested_models_and_computed_fields() -> None:
    owner = Owner(name="Rae")
    pet = Pet(owner_id=owner.id, title="rex")
    pet.hinoki_related = {"owner": owner}  # type: ignore[attr-defined]
    out = PetOut.from_model(pet, shout=pet.title.upper())
    assert isinstance(out.owner, OwnerOut) and out.owner.name == "Rae"
    assert out.shout == "REX"


def test_from_model_is_strict_about_missing_and_unloaded_data() -> None:
    pet = Pet(owner_id=uuid4(), title="rex")
    with pytest.raises(AttributeError, match="no attribute 'shout'"):
        PetOut.from_model(
            Pet(owner_id=uuid4(), title="t"), owner=OwnerOut(id=uuid4(), name="x")
        )
    with pytest.raises(RelationNotLoaded):
        PetOut.from_model(pet, shout="x")  # the relation was not loaded: no lazy I/O
    with pytest.raises(TypeError, match="no fields"):
        OwnerOut.from_model(Owner(name="x"), nope=1)


def test_contract_is_the_same_class_as_schema() -> None:
    from hinoki import Contract

    class Thing(Contract):
        name: str

    assert issubclass(Thing, Schema) and Thing.validate({"name": "x"}).name == "x"
