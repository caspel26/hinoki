"""A renderer that takes objects (``consumes_objects``, or a ``JSONRenderer`` with
``prepare``) receives a value it can read: the declared Schema, not an opaque
wrapper, wherever the result held a subclass."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hinoki import Hinoki, JSONRenderer, Schema
from hinoki.http.request import Request
from hinoki.renderers import Renderer
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-renderer"  # noqa: S105 - a marker, not a credential


class Public(Schema):
    id: int


class Private(Public):
    secret: str


def private(n: int = 1) -> Private:
    return Private(id=n, secret=SECRET)


def ids_of(value: object) -> object:
    """What a renderer that reads attributes makes of a result."""
    if isinstance(value, dict):
        return {key: ids_of(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [ids_of(item) for item in value]
    return value.id  # type: ignore[attr-defined]


class IdsJSON(JSONRenderer):
    media_type = "application/vnd.ids+json"

    def prepare(self, value: object, request: Request) -> object:
        return {"ids": ids_of(value)}


class IdsRenderer(Renderer):
    media_type = "text/x-ids"
    consumes_objects = True

    def render(self, value: object, request: Request) -> bytes:
        return json.dumps(ids_of(value)).encode()


SHAPES: dict[str, tuple[Any, Any, object]] = {
    "/one": (Public, private(1), 1),
    "/list": (list[Public], [private(1), private(2)], [1, 2]),
    "/optional": (Public | None, private(1), 1),
    "/dict": (dict[str, Public], {"a": private(1)}, {"a": 1}),
    "/grid": (list[list[Public]], [[private(1)]], [[1]]),
    "/union": (Public | list[Public], [private(1)], [1]),
}


def handler(annotation: Any, value: Any) -> Any:
    async def serve() -> Any:
        return value

    serve.__annotations__ = {"return": annotation}
    serve.__name__ = "serve"
    return serve


def build(renderer: Renderer) -> Hinoki:
    group = RouteGroup("")
    for path, (annotation, value, _) in SHAPES.items():
        group.get(path, renderers=[renderer], validate_response=False)(
            handler(annotation, value)
        )
    return Hinoki(include=[group])


def expected(path: str, renderer: Renderer) -> object:
    ids = SHAPES[path][2]
    return {"ids": ids} if isinstance(renderer, IdsJSON) else ids


@pytest.mark.parametrize("renderer", [IdsJSON(), IdsRenderer()])
@pytest.mark.parametrize("path", sorted(SHAPES))
async def test_asgi_renderer_reads_the_declared_schema(
    renderer: Renderer, path: str
) -> None:
    async with ASGIClient(build(renderer)) as client:
        reply = await client.get(path)
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == expected(path, renderer)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("renderer", [IdsJSON(), IdsRenderer()])
async def test_kiso_renderer_reads_the_declared_schema(
    renderer: Renderer, native: bool
) -> None:
    worker = KisoWorker(build(renderer), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    try:
        for path in SHAPES:
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.body, path
            assert json.loads(reply.body) == expected(path, renderer), path
    finally:
        await worker.stop()
