"""Generic schemas whose annotations are strings."""

from __future__ import annotations

from hinoki import Hinoki, Schema, computed_field, core, http
from hinoki.testing import ASGIClient


class Box[T](Schema):
    item: T
    spare: list[T] | None = None


def test_string_annotations_are_resolved_with_the_parameters() -> None:
    box = Box[int].validate({"item": 1, "spare": [2, 3]})
    assert box.item == 1
    assert box.spare == [2, 3]
    assert Box[str].specs()["item"].ftype.kind == "str"


SECRET = "s3cret"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


class Env[V](Schema):
    data: V

    @computed_field
    def first(self) -> V:
        return self.data

    @computed_field
    def maybe(self) -> V | None:
        return self.data

    @computed_field
    def many(self) -> list[V]:
        return [self.data]


def test_computed_fields_typed_by_a_parameter_resolve_under_future_annotations() -> (
    None
):
    assert Env[Public](data=Public(id=1)).dump() == {
        "data": {"id": 1},
        "first": {"id": 1},
        "maybe": {"id": 1},
        "many": [{"id": 1}],
    }
    nested = Env[Env[Public]](data=Env[Public](data=Public(id=2)))
    assert nested.dump()["first"]["first"] == {"id": 2}


def test_a_computed_field_of_a_parameterized_schema_narrows_a_subclass() -> None:
    value = Env[Public](data=Private(id=1, secret=SECRET))
    assert SECRET not in repr(value.dump())
    assert SECRET not in repr(core.Narrowed(value, Env[Public]).dump())


async def test_a_computed_field_typed_by_a_parameter_is_served_and_documented() -> None:
    @http.get("/env")
    async def env() -> Env[Public]:
        return Env[Public](data=Private(id=1, secret=SECRET))

    app = Hinoki(include=[env])
    async with ASGIClient(app) as client:
        reply = await client.get("/env")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["Env_Public_"]["properties"]["first"]["$ref"].endswith("Public")
