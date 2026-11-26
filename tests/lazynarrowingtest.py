"""A lazy projection of a Schema subclass inside a returned container is sent as
the declared Schema, on the ASGI pipeline, both Kiso lanes and the XML renderer."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

import pytest

from hinoki import Hinoki, Schema, XMLRenderer
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, field, use_database
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-lazy"  # noqa: S105 - a marker, not a credential


class LazyRow(Model, table="lazy_narrow_rows"):
    id: int = field(primary_key=True)
    name: str
    secret: str


class Public(Schema):
    id: int
    name: str


class PublicFromWide(Public):
    secret: str


class ListHolder(TypedDict):
    items: list[Public]


def lazy() -> Any:
    return LazyRow.all().aproject(PublicFromWide)


def handler(annotation: Any, wrap: Callable[[Any], Any]) -> Any:
    """A fresh handler (a route declaration marks its function) that returns the
    lazy list as ``wrap`` shapes it."""

    async def serve() -> Any:
        return wrap(await lazy())

    serve.__annotations__ = {"return": annotation}
    serve.__name__ = "serve"
    return serve


SHAPES: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "/dict": (dict[str, list[Public]], lambda rows: {"a": rows}),
    "/union": (Public | list[Public], lambda rows: rows),
    "/grid": (list[list[Public]], lambda rows: [rows]),
    "/tuple": (tuple[list[Public], ...], lambda rows: (rows,)),
    "/typeddict": (ListHolder, lambda rows: {"items": rows}),
}
ROWS = [{"id": 1, "name": "r1"}, {"id": 2, "name": "r2"}]
EXPECTED = {
    "/dict": {"a": ROWS},
    "/union": ROWS,
    "/grid": [ROWS],
    "/tuple": [ROWS],
    "/typeddict": {"items": ROWS},
}
# the tuple and TypedDict shapes cannot be checked: served unchecked
UNCHECKED = {"/tuple", "/typeddict"}


@pytest.fixture
async def url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path}/lazy.db"
    db = await Database.connect(url)
    await db.execute(
        create_table_sql("lazy_narrow_rows", table_state(LazyRow), db.dialect)
    )
    with use_database(db):
        for i in (1, 2):
            await LazyRow.acreate(id=i, name=f"r{i}", secret=SECRET)
    db.close()
    return url


def build(url: str) -> Hinoki:
    group = RouteGroup("")
    for path, (annotation, wrap) in SHAPES.items():
        group.get(path, validate_response=path not in UNCHECKED)(
            handler(annotation, wrap)
        )
    annotation, wrap = SHAPES["/dict"]
    group.get("/dict-xml", renderers=[XMLRenderer(root="r")])(handler(annotation, wrap))
    return Hinoki(modules=[DatabaseModule(url)], include=[group])


@pytest.mark.parametrize("path", sorted(SHAPES))
async def test_asgi_sends_the_declared_fields(url: str, path: str) -> None:
    async with ASGIClient(build(url)) as client:
        reply = await client.get(path)
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert json.loads(reply.raw) == EXPECTED[path]


@pytest.mark.parametrize("native", [True, False])
async def test_kiso_sends_the_declared_fields(url: str, native: bool) -> None:
    worker = KisoWorker(build(url), "127.0.0.1", 0, native_dispatch=native)
    port = await worker.start()
    try:
        for path in SHAPES:
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            assert SECRET.encode() not in reply.body, path
            assert json.loads(reply.body) == EXPECTED[path], path
    finally:
        await worker.stop()


async def test_a_lazy_projection_of_the_declared_schema_is_sent_as_it_is(
    url: str,
) -> None:
    async def serve() -> dict[str, list[Public]]:
        return {"a": await LazyRow.all().aproject(Public)}

    group = RouteGroup("")
    group.get("/exact")(serve)
    async with ASGIClient(Hinoki(modules=[DatabaseModule(url)], include=[group])) as c:
        reply = await c.get("/exact")
    assert json.loads(reply.raw) == {"a": ROWS}


async def test_the_xml_renderer_sends_the_declared_fields(url: str) -> None:
    async with ASGIClient(build(url)) as client:
        reply = await client.get("/dict-xml")
    assert reply.status == 200
    assert SECRET.encode() not in reply.raw
    assert b"r1" in reply.raw
