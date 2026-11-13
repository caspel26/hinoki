"""Regressions for the independent review of the Rust schema engine."""

import json
from typing import Annotated, Any

import pytest

from hinoki import Field, HinokiAPI, RegistrationError, Request, Schema, get, native
from hinoki.fields import FieldInfo
from hinoki.fieldtypes import check_limits
from tests.harness import Pool


class Renamed(Schema):
    a: int
    secret: str = "s3cr3t"

    def dump(self) -> dict[str, Any]:
        return {"a": self.a}


class Plain(Schema):
    a: int
    secret: str = "kept"


def test_an_overridden_dump_decides_what_is_serialised() -> None:
    assert json.loads(native.encode_json(Renamed(a=1))) == {"a": 1}
    assert json.loads(native.encode_json({"x": [Renamed(a=2)]})) == {"x": [{"a": 2}]}
    assert json.loads(native.encode_json(Plain(a=1))) == {"a": 1, "secret": "kept"}


async def test_a_response_never_leaks_what_dump_hides(pool: Pool) -> None:
    @get("/renamed")
    def renamed(request: Request) -> Renamed:
        return Renamed(a=1)

    app = HinokiAPI()
    app.include(renamed)
    client = await pool.start(app)
    assert (await client.request("GET", "/renamed")).body == {"a": 1}


@pytest.mark.parametrize(
    "pattern",
    [
        r"[A-Z]{2}\d+\Z",
        r"(?=.)\w+",
        r"(a)\1",
        r"(?x) a # comment",
        r"[a&&b]",
        r"[a~~b]",
        r"[[:alpha:]]",
        r"(?#c)a",
        "(",
    ],
)
def test_patterns_the_engine_cannot_enforce_fail_at_class_creation(
    pattern: str,
) -> None:
    with pytest.raises(RegistrationError, match="pattern"):

        class Bad(Schema):
            code: Annotated[str, Field(pattern=pattern)]


@pytest.mark.parametrize(
    "pattern",
    [r"[A-Z]{2}[0-9]+", r"[a-z0-9]+(-[a-z0-9]+)*", r"\d{4}", r"a|ab", r"(?i)abc", ""],
)
def test_ordinary_patterns_still_work_and_match_the_whole_value(pattern: str) -> None:
    class Good(Schema):
        code: Annotated[str, Field(pattern=pattern)]

    if pattern == r"a|ab":
        assert (
            Good.validate({"code": "ab"}).code == "ab"
        )  # leftmost-first must not truncate
    Good.native_schema()


@pytest.mark.parametrize("name", ["min_length", "max_length"])
@pytest.mark.parametrize("value", [2.5, 10**30, "3"])
def test_length_limits_must_be_integers(name: str, value: Any) -> None:
    with pytest.raises(RegistrationError, match=name):

        class Bad(Schema):
            text: Annotated[str, Field(**{name: value})]


@pytest.mark.parametrize("value", ["3", [1]])
def test_numeric_bounds_must_be_numbers(value: Any) -> None:
    with pytest.raises(RegistrationError, match="ge"):

        class Bad(Schema):
            n: Annotated[int, Field(ge=value)]


class Tree(Schema):
    name: str
    children: list["Tree"] = Field(default_factory=list)


def test_dump_keeps_its_documented_contract() -> None:
    class Loose(Schema):
        a: int
        b: float = 1.0

    assert Loose(a=1).dump() == {"a": 1, "b": 1.0}

    deep = Tree(name="leaf")
    for _ in range(150):
        deep = Tree(name="n", children=[deep])
    assert (
        deep.dump()["children"][0]["name"] == "n"
    )  # deeper than the JSON decoder's cap


def test_booleans_are_not_numbers_for_limits() -> None:
    # tested on the check itself: typing's Annotated cache treats True == 1
    for name in ("min_length", "max_length", "ge", "lt"):
        with pytest.raises(RegistrationError, match=name):
            check_limits(FieldInfo(**{name: True}), "x")  # type: ignore[arg-type]
