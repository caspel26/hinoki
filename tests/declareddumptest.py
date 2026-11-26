"""A declared class that overrides ``dump()`` decides the shape: a subclass that
overrides it again does not."""

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


class Holder(Schema):
    inner: Declared


def sub() -> Sub:
    return Sub(id=1, secret=SECRET)


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    return serve


SHAPES: dict[str, tuple[Any, Any]] = {
    "/one": (Declared, sub()),
    "/list": (list[Declared], [sub()]),
    "/field": (Holder, Holder(inner=sub())),
    "/dict": (dict[str, Declared], {"a": sub()}),
}


@pytest.mark.parametrize("xml", [False, True])
@pytest.mark.parametrize("path", sorted(SHAPES))
async def test_the_declared_dump_decides_over_http(path: str, xml: bool) -> None:
    group = RouteGroup("")
    options: dict[str, Any] = {"renderers": [XMLRenderer(root="r")]} if xml else {}
    annotation, value = SHAPES[path]
    group.get(path, validate_response=False, **options)(handler(annotation, value))
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get(path)
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert b"declared" in reply.raw
    if not xml:
        assert "secret" not in json.dumps(json.loads(reply.raw))


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


def test_a_view_of_a_declared_dump_uses_the_declared_dump() -> None:
    assert Narrowed(sub(), Declared).dump() == {"id": 1, "declared": True}
