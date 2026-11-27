"""``set``, ``frozenset`` and ``tuple`` Schema fields: registration, wire forms,
every refusal with its code and path, construction, output and OpenAPI."""

from __future__ import annotations

import enum
import json
import typing
from typing import Annotated, Any

import pytest
from openapi_spec_validator import validate

from hinoki import UNSET, Field, Hinoki, Schema, Unset, core, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.orm import Model, field
from hinoki.rules import Project, RuleSet, Schemas
from hinoki.schema import encode
from hinoki.testing import ASGIClient


class Point(Schema):
    x: int
    y: int = 0


class Tone(enum.Enum):
    WARM = "warm"
    COLD = "cold"


class Bag(Schema):
    tags: set[str]
    frozen: frozenset[int]
    ids: tuple[int, ...]
    pair: tuple[int, str]
    nested: tuple[int, tuple[str, ...]]


def problems(cls: type[Schema], data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        cls.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def valid(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "tags": ["b", "a"],
        "frozen": [3, 1],
        "ids": [5, 4, 5],
        "pair": [1, "one"],
        "nested": [7, ["x", "y"]],
        **changes,
    }
    return data


def test_valid_input_builds_the_declared_containers() -> None:
    bag = Bag.validate(valid())
    assert bag.tags == {"a", "b"} and type(bag.tags) is set
    assert bag.frozen == frozenset({1, 3}) and type(bag.frozen) is frozenset
    assert bag.ids == (5, 4, 5) and type(bag.ids) is tuple
    assert bag.pair == (1, "one") and type(bag.pair) is tuple
    assert bag.nested == (7, ("x", "y"))
    assert type(bag.nested[1]) is tuple


def test_duplicates_collapse_and_order_is_kept_by_tuples() -> None:
    bag = Bag.validate(valid(tags=["a", "a", "b", "a"], frozen=[]))
    assert bag.tags == {"a", "b"}
    assert bag.frozen == frozenset()
    assert Bag.validate(valid(ids=[])).ids == ()


def test_typing_aliases_are_accepted() -> None:
    class Old(Schema):
        tags: typing.Set[str]  # noqa: UP006
        pair: typing.Tuple[int, int]  # noqa: UP006
        rest: typing.Tuple[int, ...]  # noqa: UP006
        frozen: typing.FrozenSet[int]  # noqa: UP006

    old = Old.validate({"tags": ["a"], "pair": [1, 2], "rest": [3], "frozen": [4]})
    assert old.tags == {"a"} and old.pair == (1, 2)
    assert old.rest == (3,) and old.frozen == frozenset({4})


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tags": "ab"}, [("tags", "type", "expected an array")]),
        ({"frozen": {"a": 1}}, [("frozen", "type", "expected an array")]),
        ({"ids": 3}, [("ids", "type", "expected an array")]),
        ({"tags": ["a", 1]}, [("tags[1]", "type", "expected a string")]),
        ({"frozen": [1, True]}, [("frozen[1]", "type", "expected an integer")]),
        (
            {"ids": [1, "2", None]},
            [
                ("ids[1]", "type", "expected an integer"),
                ("ids[2]", "null", "null is not allowed"),
            ],
        ),
        ({"pair": [1]}, [("pair", "min_length", "at least 2 items")]),
        ({"pair": [1, "a", 3]}, [("pair", "max_length", "at most 2 items")]),
        ({"pair": []}, [("pair", "min_length", "at least 2 items")]),
        (
            {"pair": ["1", 2]},
            [
                ("pair[0]", "type", "expected an integer"),
                ("pair[1]", "type", "expected a string"),
            ],
        ),
        ({"nested": [1, [2]]}, [("nested[1][0]", "type", "expected a string")]),
        ({"nested": [1, "x"]}, [("nested[1]", "type", "expected an array")]),
        ({"pair": None}, [("pair", "null", "null is not allowed")]),
    ],
)
def test_every_refusal_has_a_code_and_a_path(
    changes: dict[str, object], expected: list[tuple[str, str, str]]
) -> None:
    assert problems(Bag, valid(**changes)) == expected


class Bounded(Schema):
    tags: Annotated[set[str], Field(min_length=1, max_length=2)]
    ids: Annotated[tuple[int, ...], Field(max_length=2)] = ()


def test_length_limits_count_the_array_as_sent() -> None:
    assert Bounded.validate({"tags": ["a", "b"]}).tags == {"a", "b"}
    assert problems(Bounded, {"tags": []}) == [
        ("tags", "min_length", "at least 1 item")
    ]
    assert problems(Bounded, {"tags": ["a", "b", "c"]}) == [
        ("tags", "max_length", "at most 2 items")
    ]
    # three entries as sent, one after the set collapses them
    assert problems(Bounded, {"tags": ["a", "a", "a"]}) == [
        ("tags", "max_length", "at most 2 items")
    ]
    assert problems(Bounded, {"tags": ["a"], "ids": [1, 2, 3]}) == [
        ("ids", "max_length", "at most 2 items")
    ]


class Shapes(Schema):
    points: tuple[Point, ...]
    segment: tuple[Point, int]
    grid: list[tuple[int, int]]
    by_name: dict[str, frozenset[int]]
    maybe: set[int] | None = None
    unset: Unset | tuple[int, ...] = UNSET
    names: set[str] = Field(default_factory=set)
    origin: tuple[int, int] = (0, 0)
    colours: frozenset[Tone] = frozenset()


def shapes(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "points": [{"x": 1}, {"x": 2, "y": 3}],
        "segment": [{"x": 0}, 5],
        "grid": [[1, 2], [3, 4]],
        "by_name": {"a": [1, 1, 2]},
        **changes,
    }
    return data


def test_containers_nest_with_schemas_lists_and_dicts() -> None:
    value = Shapes.validate(shapes())
    assert value.points == (Point(x=1), Point(x=2, y=3))
    assert value.segment == (Point(x=0), 5)
    assert value.grid == [(1, 2), (3, 4)]
    assert value.by_name == {"a": frozenset({1, 2})}
    assert value.maybe is None and value.unset is UNSET
    assert value.names == set() and value.origin == (0, 0)
    assert value.colours == frozenset()


def test_nested_errors_have_full_paths() -> None:
    found = problems(
        Shapes,
        shapes(
            points=[{"x": 1}, {"x": "no"}],
            segment=[{"x": 0}, "5"],
            grid=[[1, 2], [3]],
            by_name={"a": [1, "b"]},
        ),
    )
    assert found == [
        ("points[1].x", "type", "expected an integer"),
        ("segment[1]", "type", "expected an integer"),
        ("grid[1]", "min_length", "at least 2 items"),
        ("by_name.a[1]", "type", "expected an integer"),
    ]


def test_nullable_unset_and_defaults() -> None:
    value = Shapes.validate(shapes(maybe=[2, 1], unset=[9], colours=["warm"]))
    assert value.maybe == {1, 2} and value.unset == (9,)
    assert value.colours == frozenset({Tone.WARM})
    assert Shapes.validate(shapes(maybe=None)).maybe is None
    assert problems(Shapes, shapes(colours=["hot"])) == [
        ("colours[0]", "one_of", "must be one of 'warm', 'cold'")
    ]


def test_defaults_are_independent_per_instance() -> None:
    first, second = Shapes.validate(shapes()), Shapes.validate(shapes())
    first.names.add("x")
    assert second.names == set()


def test_construction_takes_the_declared_container_only() -> None:
    bag = Bag(
        tags={"a"}, frozen=frozenset({1}), ids=(1, 2), pair=(1, "a"), nested=(1, ())
    )
    assert bag.tags == {"a"} and bag.pair == (1, "a")
    base: dict[str, Any] = {
        "tags": {"a"},
        "frozen": frozenset({1}),
        "ids": (1,),
        "pair": (1, "a"),
        "nested": (1, ("x",)),
    }
    for change in (
        {"tags": ["a"]},
        {"tags": frozenset({"a"})},
        {"frozen": {1}},
        {"ids": [1]},
        {"pair": [1, "a"]},
        {"pair": (1,)},
        {"pair": ("1", "a")},
        {"nested": (1, ["x"])},
        {"tags": {1}},
    ):
        with pytest.raises(ValidationError):
            Bag(**{**base, **change})


def test_construction_converts_schemas_inside_containers() -> None:
    value = Shapes(
        points=(Point(x=1),),
        segment=(Point(x=2), 3),
        grid=[(1, 2)],
        by_name={"k": frozenset({1})},
    )
    assert value.points == (Point(x=1),)
    with pytest.raises(ValidationError):
        Shapes(
            points=({"x": 1},),  # type: ignore[arg-type]
            segment=(Point(x=2), 3),
            grid=[],
            by_name={},
        )


def test_output_writes_arrays_with_sets_sorted() -> None:
    bag = Bag.validate(valid())
    expected = {
        "tags": ["a", "b"],
        "frozen": [1, 3],
        "ids": [5, 4, 5],
        "pair": [1, "one"],
        "nested": [7, ["x", "y"]],
    }
    assert bag.dump() == expected
    assert encode(bag) == expected
    assert Bag.validate(bag.dump()) == bag
    kept = bag.to_dict()
    assert kept["tags"] == {"a", "b"} and type(kept["tags"]) is set
    assert type(kept["frozen"]) is frozenset
    assert kept["ids"] == (5, 4, 5) and kept["nested"] == (7, ("x", "y"))


def test_output_of_schemas_and_unsortable_items() -> None:
    value = Shapes.validate(shapes(colours=["warm", "cold"]))
    dumped = value.dump()
    assert dumped["points"] == [{"x": 1, "y": 0}, {"x": 2, "y": 3}]
    assert dumped["segment"] == [{"x": 0, "y": 0}, 5]
    assert sorted(dumped["colours"]) == ["cold", "warm"]
    assert value.to_dict()["points"] == ({"x": 1, "y": 0}, {"x": 2, "y": 3})


def test_the_json_encoder_writes_arrays_inside_schemas() -> None:
    text = bytes(core.encode_json(Bag.validate(valid()))).decode()
    assert '"tags":["a","b"]' in text
    assert '"frozen":[1,3]' in text
    assert '"pair":[1,"one"]' in text
    assert core.encode_json({"tags": {"b", "a"}}) == b'{"tags":["a","b"]}'


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (set, "unsupported"),
        (frozenset, "unsupported"),
        (tuple, "unsupported"),
        (tuple[()], "empty tuple"),
        (tuple[..., int], "Ellipsis"),  # type: ignore[misc]
        (tuple[int, ..., str], "Ellipsis"),  # type: ignore[misc]
        (set[Point], "hashable"),
        (set[list[int]], "hashable"),
        (set[dict[str, int]], "hashable"),
        (set[set[int]], "hashable"),
        (frozenset[tuple[list[int], ...]], "hashable"),
        (set[tuple[Point, int]], "hashable"),
        (set[bytearray], "unsupported"),
        (set[int, str], "takes one item type"),  # type: ignore[misc]
    ],
)
def test_unsupported_shapes_fail_at_class_creation(
    annotation: Any, message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[tuple[int, str], Field(min_length=1)],
        Annotated[tuple[int, str], Field(max_length=5)],
        Annotated[set[str], Field(ge=1)],
        Annotated[set[str], Field(one_of=("a",))],
        Annotated[set[str], Field(pattern="a")],
    ],
)
def test_constraints_that_do_not_apply_are_refused(annotation: Any) -> None:
    with pytest.raises(RegistrationError):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


def test_hashable_item_shapes_are_accepted() -> None:
    class Keys(Schema):
        pairs: set[tuple[int, str]]
        inner: frozenset[frozenset[int]]
        ids: set[Tone]

    keys = Keys.validate({"pairs": [[1, "a"], [1, "a"]], "inner": [[1]], "ids": []})
    assert keys.pairs == {(1, "a")}
    assert keys.inner == frozenset({frozenset({1})})


@pytest.mark.parametrize(
    "column", [set[str], frozenset[int], tuple[int, int], tuple[int, ...] | None]
)
def test_a_model_column_no_migration_can_create_is_refused(column: object) -> None:
    with pytest.raises(RegistrationError, match=r"Ticket\.tags: no column type"):
        type(
            "Ticket",
            (Model,),
            {
                "__annotations__": {"id": int, "tags": column},
                "id": field(primary_key=True),
            },
            table="collection_tickets",
        )


class Shade(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Palette(Schema):
    shades: set[Shade]
    groups: frozenset[frozenset[int]] = frozenset()


def test_set_output_order_is_the_same_for_members_that_do_not_sort() -> None:
    palette = Palette.validate(
        {
            "shades": ["red", "blue"],
            "groups": [[3, 1], [2], [1, 2, 3]],
        }
    )
    assert palette.dump()["shades"] == ["blue", "red"]
    assert palette.dump()["groups"] == [[1, 2, 3], [1, 3], [2]]
    assert bytes(core.encode_json(palette)).decode() == json.dumps(
        palette.dump(), separators=(",", ":")
    )


@http.post("/collections/bag")
def echo_bag(data: Bag) -> Bag:
    return data


@http.post("/collections/shapes")
def echo_shapes(data: Shapes) -> Shapes:
    return data


def test_openapi_documents_arrays() -> None:
    app = Hinoki()
    app.include(echo_bag, echo_shapes)
    document = app.openapi()
    validate(document)
    schemas = document["components"]["schemas"]
    bag = schemas["Bag"]["properties"]
    assert bag["tags"] == {
        "type": "array",
        "items": {"type": "string"},
        "uniqueItems": True,
    }
    assert bag["frozen"]["uniqueItems"] is True
    assert bag["ids"] == {"type": "array", "items": {"type": "integer"}}
    assert bag["pair"] == {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "string"}],
        "items": False,
        "minItems": 2,
        "maxItems": 2,
    }
    assert bag["nested"]["prefixItems"][1] == {
        "type": "array",
        "items": {"type": "string"},
    }
    shaped = schemas["Shapes"]["properties"]
    assert shaped["points"]["items"] == {"$ref": "#/components/schemas/Point"}
    assert shaped["segment"]["prefixItems"][0] == {"$ref": "#/components/schemas/Point"}
    assert shaped["maybe"]["anyOf"][0]["uniqueItems"] is True
    assert shaped["colours"]["items"] == {"type": "string", "enum": ["warm", "cold"]}


async def test_request_bodies_and_responses_use_arrays() -> None:
    app = Hinoki()
    app.include(echo_bag)
    async with ASGIClient(app) as client:
        reply = await client.post("/collections/bag", json=valid())
        assert reply.status == 200, reply.body
        assert reply.body == {
            "tags": ["a", "b"],
            "frozen": [1, 3],
            "ids": [5, 4, 5],
            "pair": [1, "one"],
            "nested": [7, ["x", "y"]],
        }
        reply = await client.post("/collections/bag", json=valid(pair=[1]))
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.pair", "min_length")
        ]


class Search(Schema):
    terms: set[Annotated[str, Field(max_length=10)]]
    tags: tuple[str, ...]


@http.post("/collections/search")
def search(data: Search) -> dict[str, int]:
    return {}


def test_string_limits_rule_looks_inside_containers() -> None:
    app = Hinoki()
    app.include(search)
    found = RuleSet(Schemas.require_string_limits()).run(Project(app))
    assert [d.subject.rsplit(".", 1)[-1] for d in found] == ["tags"]


class Holder(Schema):
    pair: tuple[str, Annotated[str, Field(max_length=3)]]


@http.post("/collections/holder")
def holder(data: Holder) -> dict[str, int]:
    return {}


def test_string_limits_rule_looks_at_every_tuple_position() -> None:
    app = Hinoki()
    app.include(holder)
    found = RuleSet(Schemas.require_string_limits()).run(Project(app))
    assert [d.subject.rsplit(".", 1)[-1] for d in found] == ["pair"]
