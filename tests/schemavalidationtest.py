"""Direct Schema construction and request binding share strict validation."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, ClassVar, cast
from uuid import UUID

import pytest

from hinoki import Field, Hinoki, Invalid, Schema, field_check, http, object_check
from hinoki import schema as schema_module
from hinoki.errors import ValidationError
from hinoki.testing import ASGIClient

child_checks: list[str] = []


class Child(Schema):
    display_name: Annotated[str, Field(min_length=2, alias="displayName")]

    @field_check("display_name")
    def no_reserved_name(self, value: str) -> None:
        child_checks.append(value)
        if value == "reserved":
            raise Invalid("reserved", "name is reserved")


class Envelope(Schema):
    child: Child
    count: int = 0


class TypedValues(Schema):
    identifier: UUID
    created_at: datetime


class FromRow(Schema):
    created_at: datetime
    name: Annotated[str, Field(min_length=2)]
    checks: ClassVar[list[str]] = []

    @field_check("name")
    def record_check(self, value: str) -> None:
        self.checks.append(value)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())


class InvalidDefaults(Schema):
    present: str
    bad: int = cast(int, "wrong")
    bad_factory: int = Field(default_factory=lambda: cast(int, "wrong"))


class Bag(Schema):
    values: list[int]

    @object_check
    def nonnegative(self) -> None:
        if any(value < 0 for value in self.values):
            raise Invalid("negative", "values must be nonnegative")


class BagEnvelope(Schema):
    bag: Bag


class MutatedAlias(Schema):
    display_name: Annotated[str, Field(alias="displayName", min_length=2)]

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", "x")


class AliasEnvelope(Schema):
    child: MutatedAlias


class RecursiveNode(Schema):
    children: list[RecursiveNode] = Field(default_factory=list)


from_model_checks: list[str] = []


class CountedFromModel(Schema):
    value: str
    optional: str = "default"

    @field_check("value")
    def count_check(self, value: str) -> None:
        from_model_checks.append(value)


def test_manual_construction_validates_python_values_and_nested_schemas() -> None:
    child_checks.clear()
    child = Child(display_name="valid")
    assert child_checks == ["valid"]
    assert Envelope(child=child).count == 0
    assert child_checks == ["valid", "valid"]

    with pytest.raises(ValidationError) as wrong_type:
        Child(display_name=2)  # type: ignore[arg-type]
    assert [(item.path, item.code) for item in wrong_type.value.details] == [
        ("display_name", "type")
    ]

    with pytest.raises(ValidationError) as too_short:
        Child(display_name="x")
    assert [(item.path, item.code) for item in too_short.value.details] == [
        ("display_name", "min_length")
    ]

    with pytest.raises(ValidationError) as custom:
        Child(display_name="reserved")
    assert [(item.path, item.code) for item in custom.value.details] == [
        ("display_name", "reserved")
    ]

    with pytest.raises(ValidationError) as nested:
        Envelope(child={"display_name": "valid"})  # type: ignore[arg-type]
    assert [(item.path, item.code) for item in nested.value.details] == [
        ("child", "type")
    ]

    class Parent(Schema):
        parent: str

    class CustomChild(Parent):
        child: str

        def __init__(self, *, parent: str, child: str) -> None:
            super().__init__(parent=parent)
            object.__setattr__(self, "child", child)

    assert CustomChild(parent="p", child="c").child == "c"

    class WithNew(Schema):
        value: str

        def __new__(cls, **values: object) -> WithNew:
            instance = object.__new__(cls)
            object.__setattr__(instance, "allocated", bool(values))
            return instance

    assert WithNew(value="ok").allocated is True  # type: ignore[attr-defined]
    assert WithNew.validate({"value": "ok"}).allocated is True  # type: ignore[attr-defined]
    assert WithNew.from_model(SimpleNamespace(value="row")).allocated is True  # type: ignore[attr-defined]

    typed = TypedValues(
        identifier=UUID(int=1),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert typed.identifier == UUID(int=1)
    with pytest.raises(ValidationError):
        TypedValues(identifier=str(UUID(int=1)), created_at=typed.created_at)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TypedValues(identifier=UUID(int=1), created_at=datetime(2026, 1, 1))

    FromRow.checks.clear()
    naive = datetime(2026, 1, 1)
    from_row = FromRow.from_model(SimpleNamespace(created_at=naive, name=" Aki "))
    assert from_row.created_at is naive
    assert from_row.name == "Aki"
    assert FromRow.checks == ["Aki"]

    with pytest.raises(ValidationError):
        InvalidDefaults.validate({"present": "ok"})
    with pytest.raises(ValidationError):
        InvalidDefaults(present="ok")

    bag = Bag(values=[1])
    bag.values.append(-1)
    with pytest.raises(ValidationError) as mutated:
        BagEnvelope(bag=bag)
    assert [(item.path, item.code) for item in mutated.value.details] == [
        ("bag", "negative")
    ]


def test_validation_after_post_init_preserves_nested_wire_alias_paths() -> None:
    with pytest.raises(ValidationError) as post_init:
        AliasEnvelope.validate({"child": {"displayName": "valid"}})
    assert [(item.path, item.code) for item in post_init.value.details] == [
        ("child.displayName", "min_length")
    ]


def test_manual_construction_rejects_cyclic_schema_graphs_boundedly() -> None:
    node = RecursiveNode()
    node.children.append(node)
    with pytest.raises(ValidationError) as cyclic:
        RecursiveNode(children=[node])
    assert [(item.path, item.code) for item in cyclic.value.details] == [
        ("children[0].children[0]", "depth")
    ]


def test_from_model_runs_checks_once_for_both_model_plan_paths() -> None:
    from_model_checks.clear()
    CountedFromModel.from_model(SimpleNamespace(value="omitted-default"))
    assert from_model_checks == ["omitted-default"]
    from_model_checks.clear()
    CountedFromModel.from_model(SimpleNamespace(value="complete", optional="explicit"))
    assert from_model_checks == ["complete"]


calls: list[str] = []


@http.post("/async")
async def async_create(data: Envelope) -> dict[str, bool]:
    calls.append("async")
    return {"ok": True}


@http.post("/sync")
def sync_create(data: Envelope) -> dict[str, bool]:
    calls.append("sync")
    return {"ok": True}


@pytest.mark.asyncio
async def test_invalid_body_is_rejected_before_sync_or_async_handler() -> None:
    calls.clear()
    app = Hinoki()
    app.include(async_create, sync_create)
    async with ASGIClient(app) as client:
        for path in ("/async", "/sync"):
            response = await client.post(
                path, json={"child": {"displayName": "x"}, "count": "many"}
            )
            assert response.status == 422
            assert {item["path"] for item in response.body["details"]} == {
                "data.child.displayName",
                "data.count",
            }
            assert calls == []

        for path in ("/async", "/sync"):
            response = await client.post(
                path,
                json={"child": {"displayName": "reserved"}, "count": 1},
            )
            assert response.status == 422
            assert [
                (item["path"], item["code"]) for item in response.body["details"]
            ] == [("data.child.displayName", "reserved")]
            assert calls == []

        for path in ("/async", "/sync"):
            response = await client.post(
                path, json={"child": {"displayName": "valid"}, "count": 1}
            )
            assert response.status == 200
            assert calls[-1] == path[1:]


class Plain(Schema):
    label: str


class PlainHolder(Schema):
    plain: Plain


class Node(Schema):
    name: str
    parent: Node | None = None


def test_graph_without_checks_is_walked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    walked: list[str] = []
    original = schema_module.nested_schemas

    def counting(cls: type[Schema]) -> list[type[Schema]]:
        walked.append(cls.__name__)
        return original(cls)

    monkeypatch.setattr(schema_module, "nested_schemas", counting)
    PlainHolder(plain=Plain(label="a"))
    Node(name="n", parent=Node(name="p"))
    first = len(walked)
    for _ in range(3):
        PlainHolder(plain=Plain(label="a"))
        Node(name="n", parent=Node(name="p"))
    assert len(walked) == first
    assert {"Node", "Plain", "PlainHolder"} <= set(walked)
    assert schema_module.graph_has_checks(PlainHolder) is False
    assert schema_module.graph_has_normalizers(PlainHolder) is False


def test_graph_check_added_in_a_subclass_is_not_hidden_by_the_cache() -> None:
    class Strict(Plain):
        @field_check("label")
        def not_blank(self, value: str) -> None:
            if not value:
                raise Invalid("blank", "label is blank")

    assert schema_module.graph_has_checks(Plain) is False
    assert schema_module.graph_has_checks(Strict) is True
    with pytest.raises(ValidationError):
        Strict(label="")
