"""A declared class that overrides ``dump()`` decides the shape: a subclass that
overrides it again does not, and a subclass field never reaches the output
through ``super().dump(**kw)`` at any depth or on any lane."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hinoki import Hinoki, Schema, XMLRenderer
from hinoki.core import Narrowed
from hinoki.routing import RouteGroup
from hinoki.testing import ASGIClient
from hinoki.websocket import WebSocket

SECRET = "s3cret-dump"  # noqa: S105 - a marker, not a credential


class Declared(Schema):
    id: int

    def dump(self, **_: Any) -> dict[str, Any]:
        return {"id": self.id, "declared": True}


class Sub(Declared):
    secret: str

    def dump(self, **_: Any) -> dict[str, Any]:
        return {"id": self.id, "secret": self.secret}


class Super(Schema):
    """The common "tweak the default" override."""

    id: int

    def dump(self, **kw: Any) -> dict[str, Any]:
        dumped = super().dump(**kw)
        dumped["kind"] = "super"
        return dumped


class SuperSub(Super):
    secret: str


class NoKw(Schema):
    """A ``dump()`` that cannot take the ``include`` keyword."""

    id: int

    def dump(self) -> dict[str, Any]:  # type: ignore[override]
        return {"id": self.id, "nokw": True}


class NoKwSub(NoKw):
    secret: str


def holder_of(declared: type[Schema]) -> type[Schema]:
    return type(
        f"HolderOf{declared.__name__}",
        (Schema,),
        {"__annotations__": {"inner": declared}},
    )


def listing_of(declared: type[Schema]) -> type[Schema]:
    return type(
        f"ListingOf{declared.__name__}",
        (Schema,),
        {"__annotations__": {"items": list[declared]}},  # type: ignore[valid-type]
    )


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    return serve


def shapes(declared: type[Schema], sub: type[Schema]) -> dict[str, tuple[Any, Any]]:
    def make() -> Schema:
        return sub(id=1, secret=SECRET)  # type: ignore[call-arg]

    holder, listing = holder_of(declared), listing_of(declared)
    return {
        "/one": (declared, make()),
        "/list": (list[declared], [make()]),  # type: ignore[valid-type]
        "/field": (holder, holder(inner=make())),  # type: ignore[call-arg]
        "/fieldlist": (listing, listing(items=[make()])),  # type: ignore[call-arg]
        "/dict": (dict[str, declared], {"a": make()}),  # type: ignore[valid-type]
    }


DECIDES = shapes(Declared, Sub)
SUPER = shapes(Super, SuperSub)
NOKW = shapes(NoKw, NoKwSub)


async def fetch(
    table: dict[str, tuple[Any, Any]], path: str, xml: bool, validate: bool
) -> Any:
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}
    annotation, value = table[path]
    group.get(path, validate_response=validate, **options)(handler(annotation, value))
    async with ASGIClient(Hinoki(include=[group])) as client:
        return await client.get(path)


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("path", sorted(DECIDES))
async def test_the_declared_dump_decides_over_http(path: str, xml: bool) -> None:
    reply = await fetch(DECIDES, path, xml, False)
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert b"declared" in reply.raw
    if not xml:
        assert "secret" not in json.dumps(json.loads(reply.raw))


@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("path", sorted(SUPER))
async def test_super_dump_keeps_subclass_fields_out_at_every_depth(
    path: str, xml: bool, validate: bool
) -> None:
    reply = await fetch(SUPER, path, xml, validate)
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert b"super" in reply.raw
    if not xml:
        assert "secret" not in json.dumps(json.loads(reply.raw))


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("path", sorted(NOKW))
async def test_a_dump_without_include_fails_the_same_way_at_every_depth(
    path: str, xml: bool
) -> None:
    reply = await fetch(NOKW, path, xml, False)
    assert reply.status == 500
    assert SECRET.encode() not in reply.raw


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def test_the_declared_dump_decides_for_a_reply() -> None:
    transport = FakeTransport()
    ws = WebSocket(transport, path="/w", reply=Declared)  # type: ignore[arg-type]
    await ws.send_json(sub())
    assert [json.loads(text) for text in transport.sent] == [
        {"id": 1, "declared": True}
    ]


async def test_super_dump_keeps_subclass_fields_out_of_a_reply() -> None:
    transport = FakeTransport()
    ws = WebSocket(transport, path="/w", reply=Super)  # type: ignore[arg-type]
    await ws.send_json(SuperSub(id=1, secret=SECRET))
    assert [json.loads(text) for text in transport.sent] == [{"id": 1, "kind": "super"}]


async def test_a_dump_without_include_fails_for_a_reply() -> None:
    ws = WebSocket(FakeTransport(), path="/w", reply=NoKw)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="include"):
        await ws.send_json(NoKwSub(id=1, secret=SECRET))


def sub() -> Sub:
    return Sub(id=1, secret=SECRET)


def test_a_view_of_a_declared_dump_uses_the_declared_dump() -> None:
    assert Narrowed(sub(), Declared).dump() == {"id": 1, "declared": True}


def test_a_view_of_a_super_dump_keeps_subclass_fields_out() -> None:
    view = Narrowed(SuperSub(id=1, secret=SECRET), Super)
    assert view.dump() == {"id": 1, "kind": "super"}


def test_a_view_dump_honours_the_declared_dump_for_keywords_too() -> None:
    view = Narrowed(SuperSub(id=1, secret=SECRET), Super)
    assert view.dump(include=["id"]) == {"id": 1, "kind": "super"}
    assert view.dump(exclude=["id"]) == {"kind": "super"}
    with pytest.raises(TypeError, match="controlled serialization cannot inspect"):
        view.dump(by_alias=False, exclude_unset=True)  # as for the instance itself
    with pytest.raises(ValueError, match="unknown include or exclude field: secret"):
        view.dump(include=["secret"])


def test_a_view_dump_without_include_fails_like_every_other_site() -> None:
    with pytest.raises(TypeError, match="include"):
        Narrowed(NoKwSub(id=1, secret=SECRET), NoKw).dump()
    with pytest.raises(TypeError, match="include"):
        Narrowed(NoKwSub(id=1, secret=SECRET), NoKw).dump(exclude=["id"])
