"""``dict[str, X]`` Schema fields: registration, validation, construction and output."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, Any
from uuid import UUID

import pytest
from openapi_spec_validator import validate

from hinoki import (
    Field,
    Hinoki,
    Invalid,
    Schema,
    derive,
    field_check,
    field_normalize,
    http,
)
from hinoki import schema as schema_module
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fieldtypes import MAX_MAPPING_ITEMS, MAX_MAPPING_KEY_LENGTH
from hinoki.orm import Model, field
from hinoki.rules import Project, RuleSet, Schemas
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import JSON, fetch
from tests.xmlnativelanetest import recording


class Point(Schema):
    x: int
    y: Annotated[int, Field(ge=0)] = 0

    @field_check("x")
    def not_thirteen(self, value: int) -> None:
        if value == 13:
            raise Invalid("unlucky", "13 is not allowed")


class Cat(Schema):
    kind: Annotated[str, Field(one_of=("cat",))]
    lives: int


class Dog(Schema):
    kind: Annotated[str, Field(one_of=("dog",))]
    good: bool


Pet = Annotated[Cat | Dog, Field(discriminator="kind")]
ShortKey = Annotated[str, Field(min_length=2, max_length=8, pattern="[a-z]+")]


class Labels(Schema):
    labels: dict[str, Annotated[str, Field(max_length=10)]]


class Bounded(Schema):
    tags: Annotated[dict[str, int], Field(min_length=1, max_length=2)]


class Rich(Schema):
    points: dict[str, Point] = Field(default_factory=dict)
    scores: dict[str, list[int]] = Field(default_factory=dict)
    pets: dict[str, Pet] = Field(default_factory=dict)
    maybe: dict[str, int | None] | None = None
    ids: dict[str, UUID] = Field(default_factory=dict)
    nested: dict[str, dict[str, int]] = Field(default_factory=dict)


class Keyed(Schema):
    codes: dict[ShortKey, int]


class Aliased(Schema):
    by_name: Annotated[dict[str, int], Field(alias="byName")]


def problems(cls: type[Schema], data: object) -> list[tuple[str, str, str]]:
    with pytest.raises(ValidationError) as caught:
        cls.validate(data)
    return [(d.path, d.code, d.message) for d in caught.value.details]


def test_dict_fields_register_with_a_value_type_and_bounded_keys() -> None:
    ftype = Labels.fields()["labels"].ftype
    assert ftype.kind == "dict"
    assert ftype.item is not None
    assert ftype.item.kind == "str"
    assert ftype.item.info.max_length == 10
    assert ftype.key is not None
    assert ftype.key.kind == "str"
    assert ftype.key.info.max_length is None
    assert ftype.key.input_max_length == MAX_MAPPING_KEY_LENGTH
    assert ftype.info.max_length is None
    assert ftype.input_max_length == MAX_MAPPING_ITEMS
    keyed = Keyed.fields()["codes"].ftype.key
    assert keyed is not None
    assert keyed.info.constraints() == {
        "min_length": 2,
        "max_length": 8,
        "pattern": "[a-z]+",
    }
    assert Bounded.fields()["tags"].ftype.info.max_length == 2


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (dict[int, str], r"dict keys must be str: use dict\[str, X\]"),
        (dict[UUID, str], r"dict keys must be str: use dict\[str, X\]"),
        (dict[str | None, str], r"dict keys must be str: use dict\[str, X\]"),
        (dict, r"use dict\[str, X\]"),
        (dict[str, bytearray], "unsupported type"),
        (Annotated[dict[str, int], Field(pattern="x")], "do not apply to dict"),
        (Annotated[dict[str, int], Field(one_of=({},))], "do not apply to dict"),
        (dict[Annotated[str, Field(ge=1)], int], "keys accept only"),
        (dict[Annotated[str, Field(one_of=("a",))], int], "keys accept only"),
        (dict[Annotated[str, Field(alias="k")], int], "keys accept only"),
    ],
)
def test_unsupported_dict_forms_fail_at_class_creation(
    annotation: Any, message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


def test_valid_dicts_keep_their_key_order() -> None:
    data = {"zeta": "z", "alpha": "a", "__proto__": "p", "": "empty"}
    made = Labels.validate({"labels": data})
    assert list(made.labels) == ["zeta", "alpha", "__proto__", ""]
    assert made.labels == data
    assert made.dump() == {"labels": data}
    assert list(made.dump()["labels"]) == list(data)


def test_value_problems_use_the_key_as_a_path_segment() -> None:
    assert problems(Labels, {"labels": {"a": 1, "b": "x" * 11, "c": None}}) == [
        ("labels.a", "type", "expected a string"),
        ("labels.b", "max_length", "at most 10 characters"),
        ("labels.c", "null", "null is not allowed"),
    ]
    assert problems(Labels, {"labels": ["a"]}) == [
        ("labels", "type", "expected an object"),
    ]
    assert problems(Labels, {"labels": "a"}) == [
        ("labels", "type", "expected an object"),
    ]


def test_nested_values_report_full_paths() -> None:
    assert problems(
        Rich,
        {
            "points": {"home": {"y": -1}, "work": 5},
            "scores": {"math": [1, "2"]},
            "pets": {"mugi": {"kind": "cow"}, "rex": {"kind": "dog", "good": 1}},
            "nested": {"outer": {"inner": "x"}},
        },
    ) == [
        ("points.home.x", "required", "field is required"),
        ("points.home.y", "ge", "must be greater than or equal to 0"),
        ("points.work", "type", "expected an object"),
        ("scores.math[1]", "type", "expected an integer"),
        ("pets.mugi.kind", "discriminator_unknown", "unknown discriminator value"),
        ("pets.rex.good", "type", "expected a boolean"),
        ("nested.outer.inner", "type", "expected an integer"),
    ]


def test_item_count_limits_mirror_lists() -> None:
    assert problems(Bounded, {"tags": {}}) == [
        ("tags", "min_length", "at least 1 item"),
    ]
    assert problems(Bounded, {"tags": {"a": 1, "b": 2, "c": 3}}) == [
        ("tags", "max_length", "at most 2 items"),
    ]
    assert Bounded.validate({"tags": {"a": 1, "b": 2}}).tags == {"a": 1, "b": 2}


def test_the_framework_bounds_items_and_key_length_by_default() -> None:
    many = {f"k{index}": "v" for index in range(MAX_MAPPING_ITEMS + 1)}
    assert problems(Labels, {"labels": many}) == [
        ("labels", "max_length", f"at most {MAX_MAPPING_ITEMS} items"),
    ]
    assert len(Labels.validate({"labels": dict(list(many.items())[:-1])}).labels) == (
        MAX_MAPPING_ITEMS
    )
    long_key = "k" * (MAX_MAPPING_KEY_LENGTH + 1)
    assert problems(Labels, {"labels": {long_key: "v"}}) == [
        (
            f"labels.{long_key[:64]}",
            "key_max_length",
            f"key must have at most {MAX_MAPPING_KEY_LENGTH} characters",
        ),
    ]
    edge = "é" * MAX_MAPPING_KEY_LENGTH
    assert Labels.validate({"labels": {edge: "v"}}).labels == {edge: "v"}


def test_the_default_bounds_apply_to_wire_input_only() -> None:
    many = {f"{'k' * 300}{index}": "v" for index in range(MAX_MAPPING_ITEMS + 1)}
    assert Labels(labels=many).labels == many
    assert Labels.from_model(SimpleNamespace(labels=many)).labels == many


def test_explicit_bounds_apply_everywhere() -> None:
    with pytest.raises(ValidationError):
        Bounded(tags={"a": 1, "b": 2, "c": 3})
    with pytest.raises(ValidationError):
        Bounded.from_model(SimpleNamespace(tags={"a": 1, "b": 2, "c": 3}))


def test_fields_derived_from_a_model_have_no_default_bounds() -> None:
    derived = SettingsOut.fields()["payload"].ftype
    assert derived.input_max_length is None
    assert derived.key is not None
    assert derived.key.kind == "str"
    assert derived.key.input_max_length is None
    many = {f"{'k' * 300}{index}": index for index in range(MAX_MAPPING_ITEMS + 1)}
    made = SettingsOut.validate({"id": 1, "payload": many})
    assert made.to_dict()["payload"] == many


def test_declared_key_constraints_are_enforced() -> None:
    assert Keyed.validate({"codes": {"ab": 1}}).codes == {"ab": 1}
    assert problems(Keyed, {"codes": {"a": 1, "abcdefghi": 2, "AB": "x", "ok": 3}}) == [
        ("codes.a", "key_min_length", "key must have at least 2 characters"),
        ("codes.abcdefghi", "key_max_length", "key must have at most 8 characters"),
        ("codes.AB", "key_pattern", "key must match '[a-z]+'"),
    ]


def test_null_handling_for_the_dict_and_its_values() -> None:
    assert Rich.validate({"maybe": None}).maybe is None
    assert Rich.validate({"maybe": {"a": None, "b": 1}}).maybe == {"a": None, "b": 1}
    assert problems(Labels, {"labels": None}) == [
        ("labels", "null", "null is not allowed"),
    ]


def test_nested_schemas_and_unions_are_constructed() -> None:
    made = Rich.validate(
        {
            "points": {"home": {"x": 1, "y": 2}},
            "pets": {"mugi": {"kind": "cat", "lives": 9}},
            "ids": {"me": str(UUID(int=1))},
        }
    )
    assert made.points == {"home": Point(x=1, y=2)}
    assert isinstance(made.pets["mugi"], Cat)
    assert made.ids == {"me": UUID(int=1)}
    assert made.dump()["points"] == {"home": {"x": 1, "y": 2}}
    assert made.dump()["ids"] == {"me": str(UUID(int=1))}
    assert made.to_dict()["ids"] == {"me": UUID(int=1)}
    assert made.to_dict()["points"] == {"home": {"x": 1, "y": 2}}


def test_checks_inside_dict_values_report_the_key_path() -> None:
    assert problems(Rich, {"points": {"home": {"x": 13}}}) == [
        ("points.home.x", "unlucky", "13 is not allowed"),
    ]
    with pytest.raises(ValidationError) as caught:
        Rich.validate({"points": {"a": {"x": 1}, "b": {"x": 13}}})
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("points.b.x", "unlucky")
    ]


def test_direct_construction_validates_keys_and_values() -> None:
    assert Labels(labels={"a": "x"}).labels == {"a": "x"}
    with pytest.raises(ValidationError) as caught:
        Labels(labels={"a": 1})  # type: ignore[dict-item]
    assert [(d.path, d.code) for d in caught.value.details] == [("labels.a", "type")]
    with pytest.raises(ValidationError) as caught:
        Labels(labels={1: "x"})  # type: ignore[dict-item]
    assert [(d.path, d.code, d.message) for d in caught.value.details] == [
        ("labels", "type", "object keys must be strings"),
    ]
    with pytest.raises(ValidationError) as caught:
        Rich(points={"home": {"x": 1}})  # type: ignore[dict-item]
    assert [(d.path, d.code) for d in caught.value.details] == [
        ("points.home", "type"),
    ]
    assert Rich(points={"home": Point(x=1)}).dump()["points"] == {
        "home": {"x": 1, "y": 0}
    }
    assert Rich(ids={"me": UUID(int=2)}).ids == {"me": UUID(int=2)}


def test_aliases_name_the_dict_field_only() -> None:
    made = Aliased.validate({"byName": {"by_name": 1}})
    assert made.by_name == {"by_name": 1}
    assert made.dump() == {"byName": {"by_name": 1}}
    assert problems(Aliased, {"byName": {"x": "1"}}) == [
        ("byName.x", "type", "expected an integer"),
    ]


class Settings(Model, table="mapping_settings"):
    id: int = field(primary_key=True)
    payload: dict[str, int]


@derive(Settings)
class SettingsOut(Schema):
    pass


def test_a_model_json_column_keeps_its_kind_and_maps_to_a_dict_field() -> None:
    assert Settings.__field_types__["payload"].kind == "json"
    assert SettingsOut.fields()["payload"].ftype.kind == "dict"
    row = Settings(id=1, payload={"a": 1})
    assert SettingsOut.from_model(row).dump() == {"id": 1, "payload": {"a": 1}}


def test_constraints_on_a_model_json_column_are_refused() -> None:
    with pytest.raises(RegistrationError, match="do not apply to a JSON column"):
        type(
            "BadSettings",
            (Model,),
            {
                "__annotations__": {"id": int, "payload": dict[str, int]},
                "id": field(primary_key=True),
                "payload": field(constraints=Field(max_length=2)),
            },
            table="mapping_bad_settings",
        )


@http.post("/mapping/labels")
async def echo_labels(data: Labels) -> Labels:
    return data


@http.post("/mapping/rich")
def echo_rich(data: Rich) -> Rich:
    return data


async def test_request_bodies_and_responses_carry_dicts() -> None:
    app = Hinoki()
    app.include(echo_labels, echo_rich)
    async with ASGIClient(app) as client:
        reply = await client.post(
            "/mapping/labels", json={"labels": {"b": "2", "a": "1"}}
        )
        assert reply.status == 200
        assert reply.raw.decode() == '{"labels":{"b":"2","a":"1"}}'
        reply = await client.post("/mapping/labels", json={"labels": {"a": 1}})
        assert reply.status == 422
        assert [(d["path"], d["code"]) for d in reply.body["details"]] == [
            ("data.labels.a", "type")
        ]
        reply = await client.post(
            "/mapping/labels",
            body=b'{"labels": {"a": "first", "b": "x", "a": "last"}}',
            headers={"content-type": "application/json"},
        )
        assert reply.status == 200
        assert reply.raw.decode() == '{"labels":{"a":"last","b":"x"}}'
        reply = await client.post(
            "/mapping/rich", json={"points": {"p": {"x": 1}}, "maybe": {"n": None}}
        )
        assert reply.status == 200
        assert reply.body["points"] == {"p": {"x": 1, "y": 0}}
        assert reply.body["maybe"] == {"n": None}


def test_openapi_describes_values_keys_and_item_limits() -> None:
    app = Hinoki()
    app.include(echo_labels, echo_rich, echo_keyed)
    document = app.openapi()
    validate(document)
    schemas = document["components"]["schemas"]
    assert schemas["Labels"]["properties"]["labels"] == {
        "type": "object",
        "additionalProperties": {"type": "string", "maxLength": 10},
        "propertyNames": {"type": "string"},
    }
    rich = schemas["Rich"]["properties"]
    assert rich["points"]["additionalProperties"] == {
        "$ref": "#/components/schemas/Point"
    }
    assert rich["scores"]["additionalProperties"] == {
        "type": "array",
        "items": {"type": "integer"},
    }
    assert rich["maybe"]["anyOf"][1] == {"type": "null"}
    assert rich["maybe"]["anyOf"][0]["additionalProperties"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }
    assert schemas["Keyed"]["properties"]["codes"]["propertyNames"] == {
        "type": "string",
        "minLength": 2,
        "maxLength": 8,
        "pattern": "^(?:[a-z]+)$",
    }
    assert schemas["Bounded"]["properties"]["tags"]["minProperties"] == 1


class KeyedLimits(Schema):
    keyed: Keyed
    limits: Bounded


@http.post("/mapping/keyed")
def echo_keyed(data: KeyedLimits) -> dict[str, int]:
    return {}


class FreeValues(Schema):
    notes: dict[str, str]
    grouped: dict[str, list[str]] = Field(default_factory=dict)
    fine: dict[str, Annotated[str, Field(max_length=4)]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)


@http.post("/mapping/free")
def free_values(data: FreeValues) -> dict[str, int]:
    return {}


def test_text_values_of_a_request_dict_need_a_max_length() -> None:
    app = Hinoki()
    app.include(free_values, echo_labels, echo_keyed)
    found = RuleSet(Schemas.require_string_limits()).run(Project(app))
    assert [d.subject.rsplit(".", 1)[-1] for d in found] == ["grouped", "notes"]
    assert all(d.rule_id == "HNK-SCH-002" for d in found)


async def test_the_native_lane_validates_and_encodes_dicts_like_the_python_lane() -> (
    None
):
    app = Hinoki()
    app.include(echo_labels, echo_rich)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=False)
    lanes: list[str] = []
    pipeline = worker.pipeline
    pipeline.handle_native = recording(lanes, "native", pipeline.handle_native)  # type: ignore[method-assign]
    pipeline.handle_unfinished = recording(lanes, "python", pipeline.handle_unfinished)  # type: ignore[method-assign]
    bodies = [
        ("/mapping/labels", b'{"labels": {"b": "2", "a": "1", "b": "3"}}'),
        ("/mapping/labels", b'{"labels": {"a": 1, "__proto__": "x"}}'),
        ("/mapping/rich", b'{"points": {"p": {"x": 1}}, "scores": {"s": [1, "2"]}}'),
        (
            "/mapping/rich",
            b'{"points": {"p": {"x": 1}}, "ids": {"me": "'
            + str(UUID(int=3)).encode()
            + b'"}}',
        ),
    ]
    port = await worker.start()
    try:
        native = [
            await fetch(
                port, "POST", path, JSON + f"content-length: {len(body)}\r\n", body=body
            )
            for path, body in bodies
        ]
    finally:
        await worker.stop()
    assert lanes == ["native"] * len(bodies)
    async with ASGIClient(app) as client:
        for (path, body), got in zip(bodies, native, strict=True):
            expected = await client.post(
                path, body=body, headers={"content-type": "application/json"}
            )
            assert got.status == expected.status
            if got.status == 200:
                assert got.body == expected.raw
            else:
                assert got.json()["details"] == expected.body["details"]
    assert native[0].body == b'{"labels":{"b":"3","a":"1"}}'
    assert native[1].status == 422


class Tree(Schema):
    kids: dict[str, Tree] = Field(default_factory=dict)


def test_construction_refuses_a_non_dict_and_bounds_depth() -> None:
    with pytest.raises(ValidationError) as caught:
        Labels(labels=["a"])  # type: ignore[arg-type]
    assert [(d.path, d.code) for d in caught.value.details] == [("labels", "type")]
    tree = Tree()
    for _ in range(63):  # two levels each: the Schema and its dict
        tree = Tree(kids={"k": tree})
    with pytest.raises(ValidationError) as caught:
        Tree(kids={"k": tree})
    assert [d.code for d in caught.value.details] == ["depth"]
    assert caught.value.details[0].path.endswith("k.kids")


class Titled(Schema):
    title: str
    meta: dict[str, int]

    @field_normalize("title")
    def strip_title(self, value: str) -> str:
        return value.strip()


def test_from_model_normalizes_beside_dict_fields() -> None:
    made = Titled.from_model(SimpleNamespace(title=" a ", meta={"n": 1}))
    assert (made.title, made.meta) == ("a", {"n": 1})
    with pytest.raises(ValidationError) as caught:
        Titled.from_model(SimpleNamespace(title=" a ", meta="n"))
    assert [(d.path, d.code) for d in caught.value.details] == [("meta", "type")]


class Grid(Schema):
    rows: list[list[Point]]


def test_checks_run_inside_nested_lists() -> None:
    # Before dict fields, the checks of a list nested in a list were skipped.
    assert problems(Grid, {"rows": [[{"x": 1}], [{"x": 2}, {"x": 13}]]}) == [
        ("rows[1][1].x", "unlucky", "13 is not allowed"),
    ]


class CheckedNumbers(Schema):
    numbers: list[int]
    labels: dict[str, int] = Field(default_factory=dict)
    point: Point | None = None


def test_scalar_containers_are_not_walked_for_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    walk = schema_module.value_check_problems

    def counted(*args: Any) -> Any:
        calls.append("walk")
        return walk(*args)

    monkeypatch.setattr(schema_module, "value_check_problems", counted)
    data = {"numbers": list(range(1000)), "labels": {f"k{i}": i for i in range(500)}}
    CheckedNumbers.validate({**data, "point": {"x": 1}})
    assert len(calls) <= len(CheckedNumbers.fields())


class Documents(Model, table="mapping_documents"):
    id: int = field(primary_key=True)
    entries: list[dict[str, int]]
    nested: list[list[dict[str, str]]] | None = None


def test_model_columns_holding_dicts_stay_json() -> None:
    assert Documents.__field_types__["entries"].kind == "json"
    assert Documents.__field_types__["nested"].kind == "json"
    assert Documents.__field_types__["nested"].nullable


@pytest.mark.parametrize(
    "annotation",
    [
        dict[Annotated[str, Field(min_length=300)], int],
        dict[Annotated[str, Field(min_length=5, max_length=4)], int],
        Annotated[dict[str, int], Field(min_length=2000)],
        Annotated[dict[str, int], Field(min_length=3, max_length=2)],
    ],
)
def test_impossible_dict_limits_fail_at_class_creation(annotation: Any) -> None:
    with pytest.raises(RegistrationError, match=r"min_length .* exceeds"):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


def test_keys_that_are_not_valid_text_are_type_problems() -> None:
    assert problems(Labels, {"labels": {"\ud800": "x", "ok": "y"}}) == [
        ("labels", "type", "object keys must be valid Unicode text"),
    ]
    with pytest.raises(ValidationError) as caught:
        Labels(labels={"\ud800": "x"})
    assert [(d.path, d.code) for d in caught.value.details] == [("labels", "type")]


class OddKey(str):
    __slots__ = ()

    def __hash__(self) -> int:
        return 7

    def __eq__(self, other: object) -> bool:
        return False


def test_every_entry_is_validated_once_whatever_its_key_type() -> None:
    made = Labels.validate({"labels": {OddKey("a"): "x"}})
    assert [(str(k), v) for k, v in made.labels.items()] == [("a", "x")]
    assert problems(Labels, {"labels": {OddKey("a"): 1}}) == [
        ("labels.a", "type", "expected a string"),
    ]


def test_from_model_copies_the_row_dict() -> None:
    meta = {"n": 1}
    made = Titled.from_model(SimpleNamespace(title="a", meta=meta))
    assert made.meta == meta
    assert made.meta is not meta
