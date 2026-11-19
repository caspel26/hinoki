""":meth:`Schema.replace` builds a changed, fully revalidated copy of a frozen
schema instance."""

from typing import Annotated
from uuid import UUID, uuid4

import pytest

from hinoki import Schema
from hinoki.errors import RegistrationError, ValidationError
from hinoki.schema import Field, Invalid, field_check, object_check


class Point(Schema):
    x: int
    y: int = 0


class Card(Schema):
    title: Annotated[str, Field(min_length=1, strip=True)]
    stars: Annotated[int, Field(ge=0, le=5)] = 0
    tags: list[str] = Field(default_factory=list)
    point: Point | None = None
    owner: UUID | None = None


class Range(Schema):
    low: int
    high: int

    @object_check
    def ordered(self) -> None:
        if self.low > self.high:
            raise Invalid("order", "low must not exceed high")


class Guarded(Schema):
    name: str

    @field_check("name")
    def not_admin(self, value: str) -> None:
        if value == "admin":
            raise Invalid("reserved", "reserved name")


def test_the_copy_has_the_changes_and_the_original_is_untouched() -> None:
    card = Card(title="Hello", stars=1)
    changed = card.replace(stars=4)
    assert changed.stars == 4
    assert changed.title == "Hello"
    assert card.stars == 1
    assert changed is not card
    assert type(changed) is Card


def test_no_change_gives_an_equal_copy() -> None:
    card = Card(title="Hello", tags=["a"])
    assert card.replace() == card
    assert card.replace() is not card


def test_the_changed_value_is_validated_like_any_value() -> None:
    card = Card(title="Hello")
    with pytest.raises(ValidationError) as caught:
        card.replace(stars=9)
    assert [d.path for d in caught.value.details] == ["stars"]
    with pytest.raises(ValidationError):
        card.replace(title="")


def test_normalisation_applies_to_the_changed_value() -> None:
    assert Card(title="a").replace(title="  Spaced  ").title == "Spaced"


def test_checks_see_the_whole_new_object() -> None:
    first = Range(low=1, high=5)
    assert first.replace(high=9).high == 9
    with pytest.raises(ValidationError) as caught:
        first.replace(low=7)
    assert caught.value.details[0].code == "order"
    with pytest.raises(ValidationError):
        Guarded(name="ok").replace(name="admin")


def test_an_unknown_field_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="nope"):
        Card(title="x").replace(nope=1)


def test_nested_values_and_none_can_be_set() -> None:
    owner = uuid4()
    card = Card(title="x", point=Point(x=1), owner=owner)
    moved = card.replace(point=Point(x=2, y=3))
    assert moved.point == Point(x=2, y=3)
    assert card.point == Point(x=1)
    assert card.replace(point=None).point is None
    assert card.replace(owner=None).owner is None


def test_a_mutable_default_is_not_shared_with_the_copy() -> None:
    card = Card(title="x")
    other = card.replace(stars=2)
    assert other.tags == []
    assert other.tags is not card.tags


def test_unset_fields_stay_unset_for_exclude_unset() -> None:
    card = Card(title="x")
    assert card.replace(stars=2).dump(exclude_unset=True) == {
        "title": "x",
        "stars": 2,
    }


def test_a_field_cannot_be_called_replace() -> None:
    with pytest.raises(RegistrationError, match="replace"):

        class Bad(Schema):
            replace: int  # type: ignore[assignment]
