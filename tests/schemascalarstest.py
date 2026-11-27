"""``date``, ``time``, ``timedelta``, ``Decimal`` and ``bytes`` Schema fields:
registration, wire formats, every refusal with its code and path,
construction, output, OpenAPI, rules and model columns."""

from __future__ import annotations

import base64
import datetime as dt
import random
from decimal import Decimal
from typing import Annotated, Any
from uuid import uuid4

import pytest
from openapi_spec_validator import validate

from hinoki import (
    UNSET,
    Field,
    Hinoki,
    Invalid,
    Schema,
    Unset,
    core,
    derive,
    field_check,
    http,
)
from hinoki.ai.errorcatalog import CATALOG
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fieldtypes import (
    DECIMAL_DIGITS_CEILING,
    MAX_BYTES_LENGTH,
    MAX_DECIMAL_DIGITS,
)
from hinoki.orm import Model, field
from hinoki.rules import Project, RuleSet, Schemas
from hinoki.schema import encode
from hinoki.testing import ASGIClient

UTC = dt.UTC
PLUS_TWO = dt.timezone(dt.timedelta(hours=2))
MINUS_HALF = dt.timezone(dt.timedelta(hours=-5, minutes=-30))


class Moments(Schema):
    day: dt.date
    at: dt.time
    wait: dt.timedelta
    amount: Decimal
    blob: bytes


class Price(Schema):
    amount: Decimal


def problems(cls: type[Schema], data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        cls.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def valid(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "day": "2026-11-18",
        "at": "09:30:00Z",
        "wait": "P1DT2H",
        "amount": "12.50",
        "blob": "aGlub2tp",
    }
    return {**data, **changes}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# --- registration -------------------------------------------------------------


def test_the_new_types_register_as_their_own_kinds() -> None:
    kinds = {name: spec.ftype.kind for name, spec in Moments.specs().items()}
    assert kinds == {
        "day": "date",
        "at": "time",
        "wait": "timedelta",
        "amount": "decimal",
        "blob": "bytes",
    }


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (Annotated[dt.date, Field(ge=1)], "do not apply to date"),
        (Annotated[dt.time, Field(max_length=3)], "do not apply to time"),
        (Annotated[dt.timedelta, Field(gt=0)], "do not apply to timedelta"),
        (Annotated[dt.date, Field(one_of=("2026-01-01",))], "do not apply to date"),
        (Annotated[Decimal, Field(one_of=(Decimal(1),))], "do not apply to decimal"),
        (Annotated[Decimal, Field(min_length=1)], "do not apply to decimal"),
        (Annotated[Decimal, Field(pattern="[0-9]+")], "do not apply to decimal"),
        (Annotated[bytes, Field(ge=1)], "do not apply to bytes"),
        (Annotated[bytes, Field(one_of=(b"a",))], "do not apply to bytes"),
        (Annotated[int, Field(max_digits=3)], "do not apply to int"),
        (Annotated[str, Field(decimal_places=2)], "do not apply to str"),
        (Annotated[Decimal, Field(ge=0.5)], "ge must be an int or a finite Decimal"),
        (Annotated[Decimal, Field(le=True)], "le must be an int or a finite Decimal"),
        (
            Annotated[Decimal, Field(lt=Decimal("NaN"))],
            "lt must be an int or a finite Decimal",
        ),
        (Annotated[Decimal, Field(max_digits=0)], "max_digits must be an integer"),
        (Annotated[Decimal, Field(max_digits=True)], "max_digits must be an integer"),
        (
            Annotated[Decimal, Field(max_digits=DECIMAL_DIGITS_CEILING + 1)],
            "max_digits must be an integer from 1 to 1000",
        ),
        (
            Annotated[Decimal, Field(decimal_places=-1)],
            "decimal_places must be an integer",
        ),
        (
            Annotated[Decimal, Field(max_digits=4, decimal_places=5)],
            "decimal_places 5 exceeds max_digits 4",
        ),
        (
            Annotated[Decimal, Field(decimal_places=MAX_DECIMAL_DIGITS + 1)],
            "decimal_places 65 exceeds max_digits 64",
        ),
        (
            Annotated[bytes, Field(min_length=MAX_BYTES_LENGTH + 1)],
            "min_length 65537 exceeds the maximum 65536",
        ),
        (
            Annotated[bytes, Field(min_length=5, max_length=4)],
            "min_length 5 exceeds the maximum 4",
        ),
        (Annotated[Decimal, Field(ge=Decimal(1), le=0.5)], "le must be an int"),
        (Annotated[int, Field(ge=Decimal(1))], "ge must be a number"),
    ],
)
def test_unsupported_constraints_fail_at_class_creation(
    annotation: Any, message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        type("Broken", (Schema,), {"__annotations__": {"value": annotation}})


def test_datetime_and_date_are_distinct_kinds() -> None:
    class Both(Schema):
        moment: dt.datetime
        day: dt.date

    assert Both.specs()["moment"].ftype.kind == "datetime"
    assert Both.specs()["day"].ftype.kind == "date"


# --- wire input ---------------------------------------------------------------


def test_valid_input_becomes_python_values() -> None:
    moments = Moments.validate(valid())
    assert moments.day == dt.date(2026, 11, 18)
    assert type(moments.day) is dt.date
    assert moments.at == dt.time(9, 30, tzinfo=UTC)
    assert moments.wait == dt.timedelta(days=1, hours=2)
    assert repr(moments.amount) == "Decimal('12.50')"
    assert moments.blob == b"hinoki"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-18", dt.date(2026, 11, 18)),
        ("0001-01-01", dt.date(1, 1, 1)),
        ("9999-12-31", dt.date(9999, 12, 31)),
        ("2024-02-29", dt.date(2024, 2, 29)),
    ],
)
def test_dates_are_strict_iso_8601(text: str, expected: dt.date) -> None:
    assert Moments.validate(valid(day=text)).day == expected


@pytest.mark.parametrize(
    "bad",
    [
        "2026-02-29",
        "0000-01-01",
        "2026-13-01",
        "2026-00-10",
        "2026-11-31",
        "2026-11-18T00:00:00Z",
        "20261118",
        "2026-1-18",
        "+2026-11-18",
        " 2026-11-18",
        "2026-11-18 ",
        "\uff12\uff10\uff12\uff16-11-18",
        "2026-11-1\uff18",
        "2026-W47-3",
        "2026-322",
        "",
        20261118,
        None,
        ["2026-11-18"],
    ],
)
def test_dates_refuse_every_other_shape(bad: object) -> None:
    code = "null" if bad is None else "date"
    message = (
        "null is not allowed" if bad is None else "expected an ISO 8601 date string"
    )
    assert problems(Moments, valid(day=bad)) == [("day", code, message)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09:30:00Z", dt.time(9, 30, tzinfo=UTC)),
        ("09:30:00+00:00", dt.time(9, 30, tzinfo=UTC)),
        ("09:30:00-00:00", dt.time(9, 30, tzinfo=UTC)),
        ("23:59:59.999999+02:00", dt.time(23, 59, 59, 999999, tzinfo=PLUS_TWO)),
        ("00:00:00.5-05:30", dt.time(0, 0, 0, 500000, tzinfo=MINUS_HALF)),
        (
            "12:00:00+23:59",
            dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=23, minutes=59))),
        ),
    ],
)
def test_times_carry_an_offset(text: str, expected: dt.time) -> None:
    at = Moments.validate(valid(at=text)).at
    assert at == expected
    assert at.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize(
    "bad",
    [
        "24:00:00Z",
        "09:60:00Z",
        "09:30:60Z",
        "09:30Z",
        "9:30:00Z",
        "09:30:00.Z",
        "09:30:00.1234567Z",
        "09:30:00,5Z",
        "09:30:00z",
        "09:30:00+24:00",
        "09:30:00+01:60",
        "09:30:00+0100",
        "09:30:00+01",
        "09:30:00 Z",
        "T09:30:00Z",
        "093000Z",
        "\uff109:30:00Z",
        "",
        930,
    ],
)
def test_times_refuse_every_other_shape(bad: object) -> None:
    assert problems(Moments, valid(at=bad)) == [
        ("at", "time", "expected an ISO 8601 time string")
    ]


def test_a_time_without_an_offset_is_refused_like_a_naive_datetime() -> None:
    assert problems(Moments, valid(at="09:30:00")) == [
        ("at", "time_tz", "time needs a UTC offset")
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("P1D", dt.timedelta(days=1)),
        ("PT2H", dt.timedelta(hours=2)),
        ("PT0.5S", dt.timedelta(seconds=0.5)),
        ("PT0S", dt.timedelta(0)),
        ("P0D", dt.timedelta(0)),
        ("-PT0S", dt.timedelta(0)),
        (
            "P1DT2H3M4.000005S",
            dt.timedelta(days=1, hours=2, minutes=3, seconds=4, microseconds=5),
        ),
        ("PT36H", dt.timedelta(hours=36)),
        ("PT90M", dt.timedelta(minutes=90)),
        ("PT100000S", dt.timedelta(seconds=100000)),
        ("-P1DT1S", -dt.timedelta(days=1, seconds=1)),
        ("P999999999D", dt.timedelta(days=999999999)),
        ("-P999999999D", dt.timedelta.min),
        ("P999999999DT23H59M59.999999S", dt.timedelta.max),
        ("PT000000000000000000001S", dt.timedelta(seconds=1)),
    ],
)
def test_durations_are_iso_8601(text: str, expected: dt.timedelta) -> None:
    assert Moments.validate(valid(wait=text)).wait == expected


@pytest.mark.parametrize(
    "bad",
    [
        "P",
        "PT",
        "P1DT",
        "1D",
        "P1Y",
        "P1M",
        "P1W",
        "P1DT1Y",
        "PT1D",
        "P1H",
        "PT1.5H",
        "PT1.5M",
        "PT1.1234567S",
        "PT1,5S",
        "PT.5S",
        "PT1.S",
        "p1d",
        "P1d",
        "+P1D",
        "--P1D",
        "P-1D",
        "P1D ",
        "P\uff11D",
        "PT1S1M",
        "P1DT2H2H",
        "",
        86400,
        1.5,
        True,
    ],
)
def test_durations_refuse_every_other_shape(bad: object) -> None:
    assert problems(Moments, valid(wait=bad)) == [
        ("wait", "duration", "expected an ISO 8601 duration string")
    ]


@pytest.mark.parametrize(
    "huge",
    [
        "P1000000000D",
        "-P999999999DT1S",
        "P999999999DT24H",
        "PT9999999999999999999999H",
        "PT86400000000000S",
    ],
)
def test_durations_beyond_the_timedelta_range_are_refused(huge: str) -> None:
    assert problems(Moments, valid(wait=huge)) == [
        ("wait", "duration_range", "duration is out of range")
    ]


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("12.50", "Decimal('12.50')"),
        ("0", "Decimal('0')"),
        ("-0", "Decimal('-0')"),
        ("0.000", "Decimal('0.000')"),
        ("-12345678901234567890.5", "Decimal('-12345678901234567890.5')"),
        (7, "Decimal('7')"),
        (-7, "Decimal('-7')"),
        (10**30, "Decimal('1000000000000000000000000000000')"),
    ],
)
def test_decimals_come_from_strings_and_integers(given: object, shown: str) -> None:
    assert repr(Moments.validate(valid(amount=given)).amount) == shown


@pytest.mark.parametrize(
    "bad",
    [
        1.5,
        1.0,
        True,
        "NaN",
        "nan",
        "Infinity",
        "-Infinity",
        "inf",
        "sNaN",
        "1e5",
        "1E+2",
        "1.5e-3",
        "+1",
        "01",
        "-01.5",
        ".5",
        "5.",
        "1_000",
        "1,5",
        " 1",
        "1 ",
        "\u0661\u0662",
        "\uff11\uff12",
        "--1",
        "0x10",
        "",
        [],
    ],
)
def test_decimals_refuse_floats_specials_and_loose_text(bad: object) -> None:
    assert problems(Moments, valid(amount=bad)) == [
        ("amount", "decimal", "expected a decimal string or an integer")
    ]


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("", b""),
        ("QQ==", b"A"),
        ("QUI=", b"AB"),
        ("QUJD", b"ABC"),
        ("+/+/", b"\xfb\xff\xbf"),
    ],
)
def test_bytes_are_padded_standard_base64(given: str, shown: bytes) -> None:
    assert Moments.validate(valid(blob=given)).blob == shown


@pytest.mark.parametrize(
    "bad",
    [
        "QQ",
        "QQ=",
        "QQ===",
        "QR==",
        "QUJ=",
        "-_-_",
        "QUJD\n",
        "QU JD",
        " QUJD",
        "QUJD=",
        "=QUJ",
        "Q=Q=",
        "QUJDÉ===",
        123,
        ["QUJD"],
    ],
)
def test_bytes_refuse_anything_but_canonical_base64(bad: object) -> None:
    assert problems(Moments, valid(blob=bad)) == [
        ("blob", "bytes", "expected a base64 string")
    ]


# --- constraints --------------------------------------------------------------


class Priced(Schema):
    price: Annotated[
        Decimal, Field(ge=Decimal("0.01"), lt=1000, max_digits=6, decimal_places=2)
    ]
    rate: Annotated[Decimal, Field(gt=Decimal("-1.5"), le=Decimal("1.50"))] = Decimal(0)
    places: Annotated[Decimal, Field(decimal_places=0)] = Decimal(0)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"price": "0.01"}, None),
        ({"price": "999.99"}, None),
        ({"price": "1.5"}, None),
        ({"price": "0.00"}, ("price", "ge", "must be greater than or equal to 0.01")),
        ({"price": "1000"}, ("price", "lt", "must be less than 1000")),
        (
            {"price": "10000"},
            ("price", "max_whole_digits", "at most 4 digits before the decimal point"),
        ),
        ({"price": "10000.00"}, ("price", "max_digits", "at most 6 digits")),
        ({"price": "0.001"}, ("price", "decimal_places", "at most 2 decimal places")),
        ({"price": "1.500"}, ("price", "decimal_places", "at most 2 decimal places")),
        ({"price": "999.999"}, ("price", "decimal_places", "at most 2 decimal places")),
        (
            {"price": 10000},
            ("price", "max_whole_digits", "at most 4 digits before the decimal point"),
        ),
        ({"price": 1000}, ("price", "lt", "must be less than 1000")),
        ({"price": "1", "rate": "-1.5"}, ("rate", "gt", "must be greater than -1.5")),
        (
            {"price": "1", "rate": "1.500001"},
            ("rate", "le", "must be less than or equal to 1.50"),
        ),
        ({"price": "1", "rate": "1.5"}, None),
        ({"price": "1", "rate": "-0"}, None),
        (
            {"price": "1", "places": "1.0"},
            ("places", "decimal_places", "at most 0 decimal places"),
        ),
        ({"price": "1", "places": "10"}, None),
    ],
)
def test_decimal_bounds_digits_and_places(
    data: dict[str, object], expected: tuple[str, str, str] | None
) -> None:
    if expected is None:
        Priced.validate(data)
    else:
        assert problems(Priced, data) == [expected]


def test_lt_on_a_decimal_compares_exactly() -> None:
    assert problems(Priced, {"price": "999.99", "rate": "1.51"}) == [
        ("rate", "le", "must be less than or equal to 1.50")
    ]
    places = Priced.validate({"price": "999.99"})
    assert places.price == Decimal("999.99")


def test_decimals_are_bounded_to_64_digits_by_default() -> None:
    sixty_four = "9" * MAX_DECIMAL_DIGITS
    assert Moments.validate(valid(amount=sixty_four)).amount == Decimal(sixty_four)
    assert Moments.validate(valid(amount="0." + "1" * 64)).amount
    for long in ("9" * 65, "0." + "0" * 64 + "1", "1" * 33 + "." + "1" * 32, 10**64):
        assert problems(Moments, valid(amount=long)) == [
            ("amount", "max_digits", "at most 64 digits")
        ]
    # The default has no whole-digit rule of its own: max_digits alone.
    assert Moments.validate(valid(amount="1" * 63 + ".1")).amount


def test_a_declared_max_digits_replaces_the_default() -> None:
    class Wide(Schema):
        value: Annotated[Decimal, Field(max_digits=100)]

    Wide.validate({"value": "1" * 100})
    assert problems(Wide, {"value": "1" * 101}) == [
        ("value", "max_digits", "at most 100 digits")
    ]


def test_the_digit_bound_applies_to_construction_too() -> None:
    with pytest.raises(ValidationError) as caught:
        Moments(
            day=dt.date(2026, 1, 1),
            at=dt.time(tzinfo=UTC),
            wait=dt.timedelta(0),
            amount=Decimal("1E+100"),
            blob=b"",
        )
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("amount", "max_digits")
    ]
    with pytest.raises(ValidationError) as caught:
        Moments(
            day=dt.date(2026, 1, 1),
            at=dt.time(tzinfo=UTC),
            wait=dt.timedelta(0),
            amount=Decimal("1E+999999999"),
            blob=b"",
        )
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("amount", "max_digits")
    ]


class Blobs(Schema):
    small: Annotated[bytes, Field(min_length=2, max_length=3)]
    free: bytes = b""


def test_bytes_lengths_count_decoded_bytes() -> None:
    assert Blobs.validate({"small": b64(b"ab")}).small == b"ab"
    assert Blobs.validate({"small": b64(b"abc")}).small == b"abc"
    assert problems(Blobs, {"small": b64(b"a")}) == [
        ("small", "min_length", "at least 2 bytes")
    ]
    assert problems(Blobs, {"small": b64(b"abcd")}) == [
        ("small", "max_length", "at most 3 bytes")
    ]


def test_bytes_on_wire_input_are_bounded_by_default() -> None:
    edge = b64(b"x" * MAX_BYTES_LENGTH)
    assert len(Blobs.validate({"small": "YWI=", "free": edge}).free) == MAX_BYTES_LENGTH
    assert problems(
        Blobs, {"small": "YWI=", "free": b64(b"x" * (MAX_BYTES_LENGTH + 1))}
    ) == [("free", "max_length", f"at most {MAX_BYTES_LENGTH} bytes")]
    # Values the server builds are not held to the wire-input default.
    large = Blobs(small=b"ab", free=b"x" * (MAX_BYTES_LENGTH * 2))
    assert len(large.free) == MAX_BYTES_LENGTH * 2
    with pytest.raises(ValidationError):
        Blobs(small=b"abcd")


def test_a_declared_bytes_max_length_replaces_the_default() -> None:
    class Big(Schema):
        data: Annotated[bytes, Field(max_length=MAX_BYTES_LENGTH * 2)]

    raw = b"y" * (MAX_BYTES_LENGTH * 2)
    assert Big.validate({"data": b64(raw)}).data == raw


# --- shapes around the new kinds ------------------------------------------------


class Cat(Schema):
    kind: Annotated[str, Field(one_of=("cat",))]
    born: dt.date


class Dog(Schema):
    kind: Annotated[str, Field(one_of=("dog",))]
    fee: Decimal


class Wrapped(Schema):
    maybe_day: dt.date | None = None
    patch_wait: dt.timedelta | Unset = UNSET
    days: list[dt.date] = Field(default_factory=list)
    prices: dict[str, Annotated[Decimal, Field(decimal_places=2)]] = Field(
        default_factory=dict
    )
    blobs: list[bytes | None] = Field(default_factory=list)
    opens: Annotated[dt.time, Field(alias="opensAt")] = dt.time(9, tzinfo=UTC)
    pet: Annotated[Cat | Dog | None, Field(discriminator="kind")] = None
    made: dt.date = Field(default_factory=lambda: dt.date(2026, 1, 1))


def test_nullable_unset_defaults_aliases_lists_dicts_and_unions() -> None:
    empty = Wrapped.validate({})
    assert empty.maybe_day is None
    assert empty.patch_wait is UNSET
    assert empty.opens == dt.time(9, tzinfo=UTC)
    assert empty.made == dt.date(2026, 1, 1)
    full = Wrapped.validate(
        {
            "maybe_day": "2026-01-02",
            "patch_wait": "PT1M",
            "days": ["2026-01-01", "2026-01-02"],
            "prices": {"tea": "1.20", "cake": 3},
            "blobs": ["AA==", None],
            "opensAt": "10:00:00+02:00",
            "pet": {"kind": "dog", "fee": "9.99"},
        }
    )
    assert full.days == [dt.date(2026, 1, 1), dt.date(2026, 1, 2)]
    assert full.prices == {"tea": Decimal("1.20"), "cake": Decimal(3)}
    assert full.blobs == [b"\x00", None]
    assert full.pet == Dog(kind="dog", fee=Decimal("9.99"))
    assert full.dump() == {
        "maybe_day": "2026-01-02",
        "patch_wait": "PT1M",
        "days": ["2026-01-01", "2026-01-02"],
        "prices": {"tea": "1.20", "cake": "3"},
        "blobs": ["AA==", None],
        "opensAt": "10:00:00+02:00",
        "pet": {"kind": "dog", "fee": "9.99"},
        "made": "2026-01-01",
    }
    assert "patch_wait" not in empty.dump()


def test_errors_inside_containers_have_full_paths() -> None:
    found = problems(
        Wrapped,
        {
            "days": ["2026-01-01", "nope"],
            "prices": {"tea": "1.234", "cake": 1.5},
            "blobs": ["AA==", " AA=="],
            "opensAt": "10:00:00",
            "pet": {"kind": "cat", "born": "2026-02-30"},
            "patch_wait": None,
        },
    )
    assert found == [
        ("patch_wait", "null", "null is not allowed"),
        ("days[1]", "date", "expected an ISO 8601 date string"),
        ("prices.tea", "decimal_places", "at most 2 decimal places"),
        ("prices.cake", "decimal", "expected a decimal string or an integer"),
        ("blobs[1]", "bytes", "expected a base64 string"),
        ("opensAt", "time_tz", "time needs a UTC offset"),
        ("pet.born", "date", "expected an ISO 8601 date string"),
    ]


# --- construction, output ------------------------------------------------------


def test_construction_takes_python_objects_only() -> None:
    made = Moments(
        day=dt.date(2026, 11, 18),
        at=dt.time(9, 30, tzinfo=UTC),
        wait=dt.timedelta(minutes=5),
        amount=Decimal("1.50"),
        blob=b"\x00\xff",
    )
    assert made.blob == b"\x00\xff"
    wrong: dict[str, tuple[object, str]] = {
        "day": (dt.datetime(2026, 11, 18, tzinfo=UTC), "date"),
        "at": ("09:30:00Z", "time"),
        "wait": (5, "duration"),
        "amount": (5, "decimal"),
        "blob": ("AP8=", "bytes"),
    }
    for name, (value, code) in wrong.items():
        values: dict[str, Any] = {
            "day": dt.date(2026, 11, 18),
            "at": dt.time(9, 30, tzinfo=UTC),
            "wait": dt.timedelta(minutes=5),
            "amount": Decimal("1.50"),
            "blob": b"",
            name: value,
        }
        with pytest.raises(ValidationError) as caught:
            Moments(**values)
        assert [(d.path, d.code) for d in caught.value.details] == [(name, code)], name


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("at", dt.time(9, 30), "time_tz"),
        ("amount", Decimal("NaN"), "decimal"),
        ("amount", Decimal("-Infinity"), "decimal"),
        ("amount", Decimal("sNaN"), "decimal"),
        ("blob", bytearray(b"x"), "bytes"),
        ("blob", memoryview(b"x"), "bytes"),
        ("wait", dt.timedelta.max, None),
        ("wait", dt.timedelta.min, None),
        ("amount", Decimal("1E+2"), None),
        ("amount", Decimal("1E-9"), None),
        ("at", dt.time(9, 30, tzinfo=dt.timezone(dt.timedelta(seconds=30))), "time"),
    ],
)
def test_construction_refuses_values_without_a_wire_form(
    name: str, value: object, code: str | None
) -> None:
    values: dict[str, Any] = {
        "day": dt.date(2026, 11, 18),
        "at": dt.time(9, 30, tzinfo=UTC),
        "wait": dt.timedelta(minutes=5),
        "amount": Decimal("1.50"),
        "blob": b"",
        name: value,
    }
    if code is None:
        assert getattr(Moments(**values), name) == value
        return
    with pytest.raises(ValidationError) as caught:
        Moments(**values)
    assert [(d.path, d.code) for d in caught.value.details] == [(name, code)]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (dt.timedelta(0), "PT0S"),
        (dt.timedelta(days=1), "P1D"),
        (dt.timedelta(days=-1), "-P1D"),
        (dt.timedelta(seconds=-1), "-PT1S"),
        (dt.timedelta(microseconds=1), "PT0.000001S"),
        (dt.timedelta(milliseconds=500), "PT0.5S"),
        (
            dt.timedelta(days=1, hours=2, minutes=3, seconds=4, microseconds=50),
            "P1DT2H3M4.00005S",
        ),
        (dt.timedelta(hours=25), "P1DT1H"),
        (dt.timedelta.max, "P999999999DT23H59M59.999999S"),
        (dt.timedelta.min, "-P999999999D"),
        (-dt.timedelta(days=1, microseconds=1), "-P1DT0.000001S"),
    ],
)
def test_durations_are_written_in_one_canonical_iso_form(
    value: dt.timedelta, text: str
) -> None:
    class Wait(Schema):
        wait: dt.timedelta

    assert Wait(wait=value).dump() == {"wait": text}
    assert Wait.validate({"wait": text}).wait == value
    assert encode(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("12.50"), "12.50"),
        (Decimal("-0"), "-0"),
        (Decimal("1E+2"), "100"),
        (Decimal("1E-9"), "0.000000001"),
        (Decimal("0E-3"), "0.000"),
        (Decimal(7), "7"),
    ],
)
def test_decimals_are_written_as_fixed_point_strings(value: Decimal, text: str) -> None:
    class Amount(Schema):
        amount: Decimal

    dumped = Amount(amount=value).dump()
    assert dumped == {"amount": text}
    assert Amount.validate(dumped).amount == value
    assert encode(value) == text


def test_output_formats_on_dump_to_dict_and_encode() -> None:
    made = Moments(
        day=dt.date(5, 1, 2),
        at=dt.time(9, 30, 0, 250, tzinfo=MINUS_HALF),
        wait=dt.timedelta(hours=1, seconds=0.5),
        amount=Decimal("0.10"),
        blob=b"\x00\x01\xfe\xff",
    )
    assert made.dump() == {
        "day": "0005-01-02",
        "at": "09:30:00.000250-05:30",
        "wait": "PT1H0.5S",
        "amount": "0.10",
        "blob": "AAH+/w==",
    }
    assert made.to_dict()["blob"] == b"\x00\x01\xfe\xff"
    assert made.to_dict()["amount"] == Decimal("0.10")
    assert Moments.validate(made.dump()) == made
    assert encode({"day": dt.date(2026, 1, 1), "at": dt.time(1, tzinfo=UTC)}) == {
        "day": "2026-01-01",
        "at": "01:00:00+00:00",
    }
    assert encode(b"\x00") == "AA=="
    assert core.encode_json(made) == (
        b'{"day":"0005-01-02","at":"09:30:00.000250-05:30","wait":"PT1H0.5S",'
        b'"amount":"0.10","blob":"AAH+/w=="}'
    )
    assert core.encode_json({"blob": b"\x00"}) == b'{"blob":"AA=="}'


def test_the_json_encoder_refuses_decimals_without_a_wire_form() -> None:
    made = Price(amount=Decimal("1"))
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("1E+5000")):
        object.__setattr__(made, "amount", value)
        with pytest.raises(ValueError, match="decimal"):
            core.encode_json(made)


def test_checks_receive_typed_values() -> None:
    class Booking(Schema):
        start: dt.date
        end: dt.date
        length: dt.timedelta

        @field_check("length")
        def positive(self, value: dt.timedelta) -> None:
            if value <= dt.timedelta(0):
                raise Invalid("not_positive", "must be positive")

    assert problems(
        Booking, {"start": "2026-01-01", "end": "2026-01-02", "length": "-PT1S"}
    ) == [("length", "not_positive", "must be positive")]


# --- text sources ---------------------------------------------------------------


def test_text_sources_read_the_same_text_forms() -> None:
    form = {
        "day": ["2026-11-18"],
        "at": ["09:30:00Z"],
        "wait": ["PT5M"],
        "amount": ["12.50"],
        "blob": ["aGk="],
    }
    moments = Moments.validate_text(form)
    assert moments.day == dt.date(2026, 11, 18)
    assert moments.amount == Decimal("12.50")
    assert moments.blob == b"hi"
    bad = {**form, "amount": ["1e3"], "blob": ["aGk"], "wait": ["300"]}
    with pytest.raises(ValidationError) as caught:
        Moments.validate_text(bad)
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("wait", "duration"),
        ("amount", "decimal"),
        ("blob", "bytes"),
    ]


# --- models ---------------------------------------------------------------------


class Attachment(Model, table="scalar_attachments"):
    id: int = field(primary_key=True)
    name: str
    content: bytes
    thumbnail: bytes | None = None


@derive(Attachment)
class AttachmentOut(Schema):
    pass


def test_a_bytes_column_derives_a_bytes_field_without_the_wire_default() -> None:
    specs = AttachmentOut.specs()
    assert specs["content"].ftype.kind == "bytes"
    assert specs["thumbnail"].ftype.nullable
    assert specs["content"].ftype.input_max_length is None
    big = b"z" * (MAX_BYTES_LENGTH + 10)
    out = AttachmentOut.from_model(
        Attachment(id=1, name="a", content=big, thumbnail=None)
    )
    assert getattr(out, "content") == big  # noqa: B009
    assert out.dump()["content"] == b64(big)
    assert AttachmentOut.validate(out.dump()) == out


def test_a_bytes_model_column_keeps_refusing_constraints() -> None:
    with pytest.raises(RegistrationError, match="constraints"):
        type(
            "BadBlob",
            (Model,),
            {
                "__annotations__": {
                    "id": int,
                    "data": Annotated[bytes, Field(max_length=3)],
                },
                "id": field(primary_key=True),
            },
            table="scalar_bad_blobs",
        )


# --- HTTP, OpenAPI, rules, catalogue ---------------------------------------------


@http.post("/scalars/moments")
async def echo_moments(data: Moments) -> Moments:
    return data


@http.post("/scalars/priced")
def echo_priced(data: Priced) -> Priced:
    return data


@http.post("/scalars/blobs")
def echo_blobs(data: Blobs) -> Blobs:
    return data


async def test_request_bodies_and_responses_use_the_wire_forms() -> None:
    app = Hinoki()
    app.include(echo_moments, echo_priced)
    async with ASGIClient(app) as client:
        reply = await client.post("/scalars/moments", json=valid())
        assert reply.status == 200
        assert reply.raw.decode() == (
            '{"day":"2026-11-18","at":"09:30:00+00:00","wait":"P1DT2H",'
            '"amount":"12.50","blob":"aGlub2tp"}'
        )
        reply = await client.post(
            "/scalars/moments", json=valid(amount=1.5, blob="aGk")
        )
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.amount", "decimal"),
            ("data.blob", "bytes"),
        ]
        reply = await client.post(
            "/scalars/priced",
            body=b'{"price": 12.5}',
            headers={"content-type": "application/json"},
        )
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.price", "decimal")
        ]


def test_openapi_describes_the_wire_forms() -> None:
    app = Hinoki()
    app.include(echo_moments, echo_priced, echo_blobs)
    document = app.openapi()
    validate(document)
    schemas = document["components"]["schemas"]
    properties = schemas["Moments"]["properties"]
    assert properties["day"] == {"type": "string", "format": "date"}
    assert properties["at"] == {"type": "string", "format": "time"}
    assert properties["wait"] == {"type": "string", "format": "duration"}
    assert properties["amount"] == {
        "type": "string",
        "format": "decimal",
        "pattern": "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$",
    }
    assert properties["blob"] == {
        "type": "string",
        "format": "byte",
        "contentEncoding": "base64",
    }
    price = schemas["Priced"]["properties"]["price"]
    # a string on the wire: its bounds are text extensions, not numeric keywords
    assert price["x-minimum"] == "0.01" and price["x-exclusiveMaximum"] == "1000"
    assert "minimum" not in price and "exclusiveMaximum" not in price
    rate = schemas["Priced"]["properties"]["rate"]
    assert rate["x-exclusiveMinimum"] == "-1.5" and rate["x-maximum"] == "1.50"
    assert "maxLength" not in price and "max_digits" not in price
    small = schemas["Blobs"]["properties"]["small"]
    assert "minLength" not in small and "maxLength" not in small


class Uploads(Schema):
    note: str
    raw: bytes
    amount: Decimal
    day: dt.date
    files: list[bytes]


@http.post("/scalars/uploads")
def uploads(data: Uploads) -> dict[str, int]:
    return {}


def test_string_limits_rule_does_not_flag_the_new_kinds() -> None:
    app = Hinoki()
    app.include(uploads)
    found = RuleSet(Schemas.require_string_limits()).run(Project(app))
    assert [d.subject.rsplit(".", 1)[-1] for d in found] == ["note"]


def test_every_new_code_is_explained() -> None:
    for code in (
        "date",
        "time",
        "time_tz",
        "duration",
        "duration_range",
        "decimal",
        "max_digits",
        "decimal_places",
        "max_whole_digits",
        "bytes",
    ):
        assert code in CATALOG, code


@pytest.mark.parametrize("seed", range(3))
def test_random_round_trips_keep_every_value(seed: int) -> None:
    rng = random.Random(2026 + seed)  # noqa: S311 - reproducible test data
    for _ in range(300):
        made = Moments(
            day=dt.date.fromordinal(rng.randint(1, dt.date.max.toordinal())),
            at=dt.time(
                rng.randint(0, 23),
                rng.randint(0, 59),
                rng.randint(0, 59),
                rng.choice([0, rng.randint(0, 999999)]),
                tzinfo=dt.timezone(dt.timedelta(minutes=rng.randint(-1439, 1439))),
            ),
            wait=dt.timedelta(microseconds=rng.randint(-(10**17), 10**17)),
            amount=Decimal(rng.randint(-(10**20), 10**20)).scaleb(-rng.randint(0, 20)),
            blob=uuid4().bytes[: rng.randint(0, 16)],
        )
        again = Moments.validate(made.dump())
        assert again == made
        assert repr(again) == repr(made)
