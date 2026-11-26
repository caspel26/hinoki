"""A renderer that walks attributes gets a view of the declared class at every
depth: no subclass instance, so none of its extra fields, is reachable from it."""

from __future__ import annotations

import copy
import dataclasses
import gc
import json
import pickle
from typing import Any

import pytest

from hinoki import Hinoki, Schema, computed_field
from hinoki.core import Narrowed
from hinoki.http.request import Request
from hinoki.renderers import Renderer
from hinoki.routing import RouteGroup
from hinoki.testing import ASGIClient

SECRET = "s3cret-view"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


class Holder(Schema):
    inner: Public
    items: list[Public]
    named: dict[str, Public]

    @computed_field
    def kid(self) -> Public:
        return Private(id=9, secret=SECRET)


class HolderPriv(Holder):
    extra: str = "x"


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def holder() -> HolderPriv:
    return HolderPriv(
        inner=private(1), items=[private(2)], named={"a": private(3)}, extra=SECRET
    )


def walk(value: object) -> object:
    """A generic renderer: it reads every declared field and computed value."""
    if isinstance(value, dict):
        return {key: walk(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [walk(item) for item in value]
    if isinstance(value, Schema):
        cls = value.__class__
        names = [*cls.specs(), *cls.computed_fields()]
        return {name: walk(getattr(value, name)) for name in names}
    return value


class WalkRenderer(Renderer):
    media_type = "text/x-walk"
    consumes_objects = True

    def render(self, value: object, request: Request) -> bytes:
        return json.dumps(walk(value)).encode()


EXPECTED = {
    "inner": {"id": 1},
    "items": [{"id": 2}],
    "named": {"a": {"id": 3}},
    "kid": {"id": 9},
}


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    return serve


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (Holder, holder(), EXPECTED),
        (list[Holder], [holder()], [EXPECTED]),
        (dict[str, Holder], {"a": holder()}, {"a": EXPECTED}),
    ],
    ids=["top", "list", "dict"],
)
async def test_walker_renderer_reaches_no_secret(
    annotation: Any, value: Any, expected: object
) -> None:
    group = RouteGroup("")
    group.get("/x", renderers=[WalkRenderer()], validate_response=False)(
        handler(annotation, value)
    )
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/x")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == expected


def view() -> Any:
    return Narrowed(holder(), Holder)


def test_attributes_of_a_view_are_views() -> None:
    held = view()
    for leaf in (held.inner, held.items[0], held.named["a"], held.kid):
        assert type(leaf) is Narrowed
        assert leaf.__class__ is Public  # type: ignore[comparison-overlap]
        assert leaf.id in {1, 2, 3, 9}
        with pytest.raises(AttributeError):
            leaf.secret  # noqa: B018
    with pytest.raises(AttributeError):
        held.extra  # noqa: B018


def reachable_text(value: Any) -> str:
    seen = [
        repr(getattr(value, "__dict__", None)),
        repr(vars(value) if hasattr(value, "__dict__") else None),
    ]
    for probe in (copy.copy, copy.deepcopy, pickle.dumps, dataclasses.asdict):
        try:
            seen.append(repr(probe(value)))
        except Exception as exc:
            seen.append(type(exc).__name__)
    seen.append(repr(gc.get_referents(value)))
    seen.append(repr(value.dump()))
    return " ".join(seen)


@pytest.mark.parametrize("pick", ["inner", "kid"])
def test_nothing_reaches_the_secret_from_a_view(pick: str) -> None:
    held = view()
    assert SECRET not in reachable_text(getattr(held, pick))
    assert SECRET not in reachable_text(held.items[0])
    assert SECRET not in reachable_text(held)


def test_a_view_needs_a_class_the_value_derives_from() -> None:
    with pytest.raises(TypeError):
        Narrowed(private(), int)
    with pytest.raises(TypeError):
        Narrowed(private(), Holder)
    with pytest.raises(TypeError):
        Narrowed(1, Public)
    assert Narrowed(private(), Public).id == 1


def test_a_view_dumps_with_the_keywords_of_schema_dump() -> None:
    held = Narrowed(private(7), Public)
    assert held.dump() == {"id": 7}
    assert held.dump(include=["id"]) == {"id": 7}
    assert held.dump(exclude=["id"]) == {}
    assert held.dump(by_alias=False) == {"id": 7}
    assert held.dump(exclude_unset=True) == {"id": 7}
    with pytest.raises(ValueError, match="unknown"):
        held.dump(include=["secret"])
    with pytest.raises(TypeError):
        held.dump(include="id")
