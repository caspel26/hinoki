"""A ``float`` field holds a ``float`` when a ``Schema`` is built in Python."""

from __future__ import annotations

import pytest

from hinoki import Field, Schema
from hinoki.errors import ValidationError


class Reading(Schema):
    value: float
    maybe: float | None = None
    samples: list[float] = Field(default_factory=list)
    pair: tuple[float, float] = (0.0, 0.0)
    by_name: dict[str, float] = Field(default_factory=dict)
    label: str = ""
    count: int = 0


class Holder(Schema):
    reading: Reading


def test_an_int_in_a_float_field_is_stored_as_a_float() -> None:
    reading = Reading(value=1, maybe=2)
    assert type(reading.value) is float and reading.value == 1.0
    assert type(reading.maybe) is float


def test_floats_inside_containers_are_converted() -> None:
    reading = Reading(samples=[1, 2.5], pair=(1, 2), by_name={"a": 3}, value=0)
    assert [type(item) for item in reading.samples] == [float, float]
    assert [type(item) for item in reading.pair] == [float, float]
    assert type(reading.by_name["a"]) is float


def test_other_fields_and_booleans_are_left_alone() -> None:
    reading = Reading(value=1, count=2)
    assert type(reading.count) is int and reading.label == ""
    assert Reading.validate({"value": 1}).value == 1.0


def test_a_nested_schema_normalizes_its_own_floats() -> None:
    holder = Holder(reading=Reading(value=4))
    assert type(holder.reading.value) is float


def test_a_boolean_is_still_refused() -> None:
    with pytest.raises(ValidationError):
        Reading(value=True)
