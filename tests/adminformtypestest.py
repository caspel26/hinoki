"""Admin forms for date, time, Decimal and Enum columns."""

import datetime
import enum
from decimal import Decimal

import pytest

from hinoki.admin.forms import (
    FieldProblem,
    FormField,
    collect,
    control,
    convert,
    fields_for,
    input_text,
)
from hinoki.orm import Model, field


class Level(enum.Enum):
    LOW = 1
    HIGH = 2


class Ticket(Model, table="admin_form_tickets"):
    id: int = field(primary_key=True)
    day: datetime.date
    at: datetime.time
    price: Decimal
    level: Level
    note_level: Level | None = None


def fields() -> dict[str, FormField]:
    return {f.name: f for f in fields_for(Ticket, creating=True)}


def test_supported_column_types_become_editable_inputs() -> None:
    found = fields()
    assert {n: (f.kind, f.shown_only) for n, f in found.items() if n != "id"} == {
        "day": ("date", False),
        "at": ("time", False),
        "price": ("decimal", False),
        "level": ("enum", False),
        "note_level": ("enum", False),
    }
    assert 'type="date"' in control(found["day"], None, None)
    assert 'type="time"' in control(found["at"], None, None)
    assert 'type="text"' in control(found["price"], None, None)


def test_values_convert_and_unchanged_values_are_left_out_on_edit() -> None:
    found = fields()
    form = {
        "day": "2026-11-13",
        "at": "09:30",
        "price": "12.50",
        "level": "HIGH",
        "note_level": "",
    }
    values, errors = collect(list(found.values()), {**form, "id": "1"})
    assert errors == {}
    assert values["day"] == datetime.date(2026, 11, 13)
    assert values["at"] == datetime.time(9, 30)
    assert values["price"] == Decimal("12.50")
    assert values["level"] is Level.HIGH and values["note_level"] is None
    original = {k: values[k] for k in ("day", "at", "price", "level", "note_level")}
    assert collect(list(found.values()), form, original) == ({}, {})
    retyped = {**form, "at": "09:30:00", "price": "12.5"}
    assert collect(list(found.values()), retyped, original) == ({}, {})
    broken = {**form, "at": "later"}
    assert collect(list(found.values()), broken, original)[1] == {
        "at": "use a time such as 09:30"
    }


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("day", "13/11/2026", "date such as"),
        ("at", "9h", "time such as"),
        ("at", "09:30+01:00", "without a UTC offset"),
        ("price", "abc", "decimal number"),
        ("price", "NaN", "decimal number"),
        ("price", "1" * 101, "decimal number"),
        ("level", "MEDIUM", "choose one"),
        ("level", "", "required"),
    ],
)
def test_bad_values_report_one_message(name: str, text: str, message: str) -> None:
    with pytest.raises(FieldProblem, match=message):
        convert(fields()[name], text)


def test_enum_select_lists_members_and_keeps_the_chosen_one() -> None:
    html = str(control(fields()["level"], Level.HIGH, "choose one"))
    assert '<option value="HIGH" selected>HIGH</option>' in html
    assert 'value="LOW"' in html and "aria-invalid" in html
    assert '<option value="">—</option>' in str(
        control(fields()["note_level"], None, None)
    )


def test_times_and_enums_show_as_form_text() -> None:
    assert input_text(datetime.time(9, 30, 5)) == "09:30:05"
    assert input_text(Level.LOW) == "LOW"
