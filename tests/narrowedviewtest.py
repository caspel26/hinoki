"""A renderer that walks attributes gets a view of the declared class at every
depth: no subclass instance, so none of its extra fields, is reachable from it."""

from __future__ import annotations

import copy
import dataclasses
import gc
import json
import pickle
from typing import Any, TypedDict

import pytest

from hinoki import Hinoki, JSONRenderer, Schema, computed_field, derive
from hinoki.core import Narrowed
from hinoki.http.request import Request
from hinoki.orm import Model, pk
from hinoki.pagination import Page
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


def exact_holder() -> Holder:
    return Holder(inner=private(1), items=[private(2)], named={"a": private(3)})


class Outer(Schema):
    mid: Holder


class Box(TypedDict):
    holder: Holder


def walk(value: object) -> object:
    """A generic renderer: it reads every declared field and computed value."""
    out: object = value
    if isinstance(value, dict):
        out = {key: walk(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        out = [walk(item) for item in value]
    elif isinstance(value, Page):
        out = {"items": walk(value.items), "total": value.total}
    elif isinstance(value, Schema):
        cls = value.__class__
        names = [*cls.specs(), *cls.computed_fields()]
        out = {name: walk(getattr(value, name)) for name in names}
    return out


class WalkRenderer(Renderer):
    media_type = "text/x-walk"
    consumes_objects = True

    def render(self, value: object, request: Request) -> bytes:
        return json.dumps(walk(value)).encode()


class WalkJSON(JSONRenderer):
    media_type = "application/vnd.walk+json"

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[type[object]] = []

    def prepare(self, value: object, request: Request) -> object:
        self.seen.append(type(value))
        return walk(value)


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


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (Holder, exact_holder(), EXPECTED),
        (Holder | None, exact_holder(), EXPECTED),
        (list[Holder], [exact_holder()], [EXPECTED]),
        (
            Page[Holder],
            Page([exact_holder()], 1, 10, 1),
            {"items": [EXPECTED], "total": 1},
        ),
        (Outer, Outer(mid=exact_holder()), {"mid": EXPECTED}),
    ],
    ids=["top", "optional", "list", "page", "nested"],
)
async def test_walker_renderer_wraps_exact_instances_with_nested_subclasses(
    annotation: Any, value: Any, expected: object, validate: bool
) -> None:
    group = RouteGroup("")
    group.get("/x", renderers=[WalkRenderer()], validate_response=validate)(
        handler(annotation, value)
    )
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/x")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == expected


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (dict[str, Holder], {"a": exact_holder()}, {"a": EXPECTED}),
        (Box, {"holder": exact_holder()}, {"holder": EXPECTED}),
    ],
    ids=["dict", "typeddict"],
)
async def test_walker_renderer_wraps_exact_instances_in_guide_containers(
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


@pytest.mark.parametrize("validate", [True, False])
async def test_json_prepare_receives_narrowed_view_for_exact_holder(
    validate: bool,
) -> None:
    renderer = WalkJSON()
    group = RouteGroup("")
    group.get("/x", renderers=[renderer], validate_response=validate)(
        handler(Holder, exact_holder())
    )
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/x")
    assert reply.status == 200
    assert renderer.seen == [Narrowed]
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == EXPECTED


class ViewItem(Model, table="narrowed_view_items"):
    id: int = pk()
    name: str


@derive(ViewItem, only=("id", "name"))
class DerivedItem(Schema):
    @computed_field
    def kid(self) -> Public:
        return private(9)


class StrictDump(Schema):
    id: int

    def dump(self) -> dict[str, Any]:  # type: ignore[override]
        return {"id": self.id, "v": 1}


class StrictDumpSub(StrictDump):
    secret: str = SECRET


class MediaOnlyJSON(JSONRenderer):
    media_type = "application/vnd.mediaonly+json"


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (
            list[Public | int] | list[Public | Holder],
            [Public(id=1), holder()],
            [{"id": 1}, EXPECTED],
        ),
        (
            dict[str, Public | int] | dict[str, Public | Holder],
            {"a": Public(id=1), "b": holder()},
            {"a": {"id": 1}, "b": EXPECTED},
        ),
    ],
    ids=["list-union", "dict-union"],
)
async def test_overlapping_union_narrows_all_elements(
    annotation: Any, value: Any, expected: object
) -> None:
    walk_json = WalkJSON()
    group = RouteGroup("")
    group.get("/stock", validate_response=True)(handler(annotation, value))
    group.get("/walk", renderers=[WalkRenderer()], validate_response=True)(
        handler(annotation, value)
    )
    group.get("/json-sub", renderers=[walk_json], validate_response=True)(
        handler(annotation, value)
    )
    async with ASGIClient(Hinoki(include=[group])) as client:
        for path in ("/stock", "/walk", "/json-sub"):
            reply = await client.get(path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.raw, path
            assert json.loads(reply.raw) == expected, path


async def test_model_return_projected_to_derived_schema_wraps_view() -> None:
    renderer = WalkJSON()
    group = RouteGroup("")
    group.get("/walk", renderers=[WalkRenderer()], validate_response=True)(
        handler(DerivedItem, ViewItem(id=1, name="ada"))
    )
    group.get("/json-sub", renderers=[renderer], validate_response=True)(
        handler(DerivedItem, ViewItem(id=1, name="ada"))
    )
    expected = {"id": 1, "name": "ada", "kid": {"id": 9}}
    async with ASGIClient(Hinoki(include=[group])) as client:
        for path in ("/walk", "/json-sub"):
            reply = await client.get(path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.raw, path
            assert json.loads(reply.raw) == expected, path
    assert renderer.seen == [Narrowed]


async def test_exact_schema_with_no_kwargs_dump_works_under_json_subclass() -> None:
    group = RouteGroup("")
    group.get("/media", renderers=[MediaOnlyJSON()], validate_response=True)(
        handler(StrictDump, StrictDump(id=1))
    )
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/media")
    assert reply.status == 200
    assert json.loads(reply.raw) == {"id": 1, "v": 1}
    assert Narrowed(StrictDump(id=1), StrictDump).dump() == {"id": 1, "v": 1}
    with pytest.raises(TypeError, match="include"):
        Narrowed(StrictDumpSub(id=1), StrictDump).dump()


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
