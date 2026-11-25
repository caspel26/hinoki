"""A handler result is sent as its declared Schema, in every lane: a subclass
instance loses its extra fields whether it is returned alone, optionally, in a
``list``, in a ``Page`` / ``CursorPage``, or as a lazy projection. A Schema nested
inside another Schema's field is not covered."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, Schema, core
from hinoki.cli.routes import lanes_of
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, pk, use_database
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.orm.projection import _ProjectedList, _ProjectedPage
from hinoki.pagination import CursorPage, Page, PageParams
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import narrow_items, narrow_page
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-value"  # noqa: S105 - a marker, not a credential


class NarrowRow(Model, table="narrow_rows"):
    id: int = pk()
    name: str
    secret: str


class Public(Schema):
    id: int
    name: str


class Private(Public):
    secret: str


def private(n: int = 1) -> Private:
    return Private(id=n, name=f"n{n}", secret=SECRET)


def public(n: int = 1) -> Public:
    return Public(id=n, name=f"n{n}")


def plain_routes(group: RouteGroup) -> None:

    @group.get("/single", validate_response=True)
    async def single() -> Public:
        return private()

    @group.get("/list", validate_response=True)
    async def as_list() -> list[Public]:
        return [private(1), private(2)]

    @group.get("/mixed", validate_response=True)
    async def mixed() -> list[Public]:
        return [public(1), private(2), public(3)]

    @group.get("/exact", validate_response=True)
    async def exact() -> list[Public]:
        return [public(1), public(2)]

    @group.get("/optional", validate_response=True)
    async def optional() -> list[Public] | None:
        return [private()]

    @group.get("/single-optional", validate_response=True)
    async def single_optional() -> Public | None:
        return private()

    @group.get("/page", validate_response=True)
    async def page() -> Page[Public]:
        return Page([private(1), public(2)], 1, 2, 2)

    @group.get("/cursor", validate_response=True)
    async def cursor() -> CursorPage[Public]:
        return CursorPage([private(1), public(2)], None, None, 2)


def projected_routes(group: RouteGroup) -> None:
    @group.get("/projected-list", validate_response=True)
    async def projected_list() -> list[Public]:
        return await NarrowRow.all().order_by("id").aproject(Private)

    @group.get("/projected-exact", validate_response=True)
    async def projected_exact() -> list[Public]:
        return await NarrowRow.all().order_by("id").aproject(Public)

    @group.get("/projected-page", validate_response=True)
    async def projected_page() -> Page[Public]:
        return (
            await NarrowRow.all().order_by("id").apaginate_as(Private, PageParams(1, 2))
        )

    @group.get("/projected-item", validate_response=True)
    async def projected_item() -> Public:
        return await NarrowRow.filter(id=1).aget_as(Private)


def build_app(url: str) -> Hinoki:
    group = RouteGroup("")
    plain_routes(group)
    projected_routes(group)
    return Hinoki(modules=[DatabaseModule(url)], include=[group])


def declared(*ids: int) -> list[dict[str, Any]]:
    return [{"id": n, "name": f"n{n}"} for n in ids]


EXPECTED: dict[str, object] = {
    "/single": {"id": 1, "name": "n1"},
    "/single-optional": {"id": 1, "name": "n1"},
    "/list": declared(1, 2),
    "/mixed": declared(1, 2, 3),
    "/exact": declared(1, 2),
    "/optional": declared(1),
    "/page": {
        "items": declared(1, 2),
        "page": 1,
        "page_size": 2,
        "total": 2,
        "pages": 1,
    },
    "/cursor": {
        "items": declared(1, 2),
        "next": None,
        "previous": None,
        "page_size": 2,
    },
    "/projected-list": declared(1, 2, 3),
    "/projected-exact": declared(1, 2, 3),
    "/projected-page": {
        "items": declared(1, 2),
        "page": 1,
        "page_size": 2,
        "total": 3,
        "pages": 2,
    },
    "/projected-item": {"id": 1, "name": "n1"},
}
QUERIES = {"/projected-page": "page=1&page_size=2"}


@pytest.fixture
async def url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'narrowing.sqlite3'}"
    database = await Database.connect(url)
    await database.execute(
        create_table_sql("narrow_rows", table_state(NarrowRow), database.dialect)
    )
    with use_database(database):
        for n in (1, 2, 3):
            await NarrowRow.acreate(id=n, name=f"n{n}", secret=SECRET)
    database.close()
    return url


async def bodies_over_asgi(url: str) -> dict[str, bytes]:
    async with ASGIClient(build_app(url)) as client:
        out = {}
        for path in EXPECTED:
            reply = await client.get(path, query=QUERIES.get(path, ""))
            assert reply.status == 200, path
            out[path] = reply.raw
        return out


async def bodies_over_kiso(url: str, *, native_dispatch: bool) -> dict[str, bytes]:
    app = build_app(url)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native_dispatch)
    port = await worker.start()
    try:
        if native_dispatch:
            lanes = lanes_of(app)
            assert {lanes[id(r)][0] for r in app.routes} == {"native"}
        out = {}
        for path in EXPECTED:
            reply = await fetch(
                port, "GET", f"{path}?{QUERIES[path]}" if path in QUERIES else path
            )
            assert reply.status == 200, path
            out[path] = reply.body
        return out
    finally:
        await worker.stop()


def check(bodies: dict[str, bytes]) -> None:
    for path, body in bodies.items():
        assert SECRET.encode() not in body, path
        assert json.loads(body) == EXPECTED[path], path


async def test_asgi_pipeline_narrows_every_shape(url: str) -> None:
    check(await bodies_over_asgi(url))


async def test_native_lane_narrows_every_shape(url: str) -> None:
    check(await bodies_over_kiso(url, native_dispatch=True))


async def test_python_lane_matches_native_lane_bytes(url: str) -> None:
    python = await bodies_over_kiso(url, native_dispatch=False)
    check(python)
    assert python == await bodies_over_kiso(url, native_dispatch=True)


async def test_exact_projection_keeps_its_pre_encoded_handle(url: str) -> None:
    database = await Database.connect(url)
    with use_database(database):
        exact = await NarrowRow.all().order_by("id").aproject(Public)
        paged = await NarrowRow.all().apaginate_as(Public, PageParams(1, 2))
        assert isinstance(exact, _ProjectedList) and isinstance(paged, _ProjectedPage)
        assert narrow_items(Public, exact) is exact
        assert narrow_page(Public, paged) is paged
        assert exact._projected_handle is not None
        assert paged._projected_handle is not None
    database.close()


def test_all_exact_accepts_only_an_exact_list_of_exact_items() -> None:
    class Lazy(list[Public]):  # a list subclass may keep its items elsewhere
        pass

    assert core.all_exact([], Public)
    assert core.all_exact([public(1), public(2)], Public)
    assert not core.all_exact([public(1), private(2)], Public)
    assert not core.all_exact([private(1)], Public)
    assert not core.all_exact(Lazy([public(1)]), Public)
    assert not core.all_exact((public(1),), Public)
    assert not core.all_exact(public(1), Public)
