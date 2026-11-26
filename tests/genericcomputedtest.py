"""A computed field typed with a type parameter is narrowed by the argument of
the parameterized class: ``-> T`` on ``Envelope[Public]`` is ``-> Public``."""

from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

import pytest

from hinoki import Hinoki, Schema, XMLRenderer, computed_field, core
from hinoki.routing import RouteGroup
from hinoki.testing import ASGIClient

SECRET = "s3cret-generic"  # noqa: S105 - a marker, not a credential


T = TypeVar("T")


class Public(Schema):
    id: int


class Private(Public):
    secret: str


class Envelope(Schema, Generic[T]):  # noqa: UP046 - Schema generics take TypeVar parameters
    data: T

    @computed_field
    def first(self) -> T:
        return Private(id=1, secret=SECRET)  # type: ignore[return-value]

    @computed_field
    def maybe(self) -> T | None:
        return Private(id=2, secret=SECRET)  # type: ignore[return-value]

    @computed_field
    def many(self) -> list[T]:
        return [Private(id=3, secret=SECRET)]  # type: ignore[list-item]


class Sub(Envelope[Public]):
    pass


EXPECTED = {
    "data": {"id": 0},
    "first": {"id": 1},
    "maybe": {"id": 2},
    "many": [{"id": 3}],
}


@pytest.mark.parametrize("klass", [Envelope[Public], Sub])
def test_the_computed_values_are_written_as_the_argument(klass: Any) -> None:
    value = klass(data=Public(id=0))
    assert value.dump() == EXPECTED
    assert json.loads(core.encode_json(value, None, False, None)) == EXPECTED
    assert SECRET not in repr(value.to_dict())


@pytest.mark.parametrize("xml", [False, True])
async def test_the_computed_values_are_narrowed_over_http(xml: bool) -> None:
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}

    async def serve() -> object:
        return Envelope[Public](data=Public(id=0))

    serve.__annotations__ = {"return": Envelope[Public]}
    group.get("/e", **options)(serve)
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/e")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
