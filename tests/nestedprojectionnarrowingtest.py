"""A lazy projection (`aget_as`, `aproject`) held in a Schema field is sent as the
field's declared type, in every lane: its pre-encoded bytes are used only when
it is exactly that Schema."""

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
from hinoki.routing import RouteGroup
from hinoki.runtime.kiso import KisoWorker
from hinoki.testing import ASGIClient
from tests.kisoservertest import fetch

SECRET = "s3cret-value"  # noqa: S105 - a marker, not a credential


class Gadget(Model, table="nested_projection_gadgets"):
    id: int = pk()
    name: str
    secret: str


class GPub(Schema):
    id: int
    name: str


class GPriv(GPub):
    secret: str


class One(Schema):
    item: GPub


class Opt(Schema):
    item: GPub | None


class Many(Schema):
    items: list[GPub]


def routes(group: RouteGroup) -> None:
    @group.get("/one", validate_response=True)
    async def one() -> One:
        return One(item=await Gadget.filter(id=1).aget_as(GPriv))

    @group.get("/one-exact", validate_response=True)
    async def one_exact() -> One:
        return One(item=await Gadget.filter(id=1).aget_as(GPub))

    @group.get("/opt", validate_response=False)
    async def opt() -> Opt:
        return Opt(item=await Gadget.filter(id=1).aget_as(GPriv))

    @group.get("/many-list", validate_response=True)
    async def many_list() -> Many:
        return Many(items=await Gadget.all().order_by("id").aproject(GPriv))

    @group.get("/many-items", validate_response=True)
    async def many_items() -> Many:
        return Many(items=[await Gadget.filter(id=1).aget_as(GPriv)])

    @group.get("/top", validate_response=True)
    async def top() -> GPub:
        return await Gadget.filter(id=1).aget_as(GPriv)


ONE = {"id": 1, "name": "n1"}
EXPECTED: dict[str, object] = {
    "/one": {"item": ONE},
    "/one-exact": {"item": ONE},
    "/opt": {"item": ONE},
    "/many-list": {"items": [{"id": n, "name": f"n{n}"} for n in (1, 2)]},
    "/many-items": {"items": [ONE]},
    "/top": ONE,
}


@pytest.fixture
async def url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'nested.sqlite3'}"
    database = await Database.connect(url)
    await database.execute(
        create_table_sql(
            "nested_projection_gadgets", table_state(Gadget), database.dialect
        )
    )
    with use_database(database):
        for n in (1, 2):
            await Gadget.acreate(id=n, name=f"n{n}", secret=SECRET)
    database.close()
    return url


def build_app(url: str) -> Hinoki:
    group = RouteGroup("")
    routes(group)
    return Hinoki(modules=[DatabaseModule(url)], include=[group])


def check(bodies: dict[str, bytes]) -> None:
    assert bodies.keys() == EXPECTED.keys()
    for path, body in bodies.items():
        assert SECRET.encode() not in body, path
        assert json.loads(body) == EXPECTED[path], path


async def over_asgi(url: str) -> dict[str, bytes]:
    async with ASGIClient(build_app(url)) as client:
        out = {}
        for path in EXPECTED:
            reply = await client.get(path)
            assert reply.status == 200, path
            out[path] = reply.raw
        return out


async def over_kiso(url: str, *, native_dispatch: bool) -> dict[str, bytes]:
    app = build_app(url)
    worker = KisoWorker(app, "127.0.0.1", 0, native_dispatch=native_dispatch)
    port = await worker.start()
    try:
        if native_dispatch:
            assert {lanes_of(app)[id(r)][0] for r in app.routes} == {"native"}
        out = {}
        for path in EXPECTED:
            reply = await fetch(port, "GET", path)
            assert reply.status == 200, path
            out[path] = reply.body
        return out
    finally:
        await worker.stop()


async def test_asgi_pipeline_narrows_a_projection_in_a_field(url: str) -> None:
    check(await over_asgi(url))


async def test_native_lane_narrows_a_projection_in_a_field(url: str) -> None:
    check(await over_kiso(url, native_dispatch=True))


async def test_python_lane_matches_the_other_lanes(url: str) -> None:
    python = await over_kiso(url, native_dispatch=False)
    check(python)
    assert python == await over_kiso(url, native_dispatch=True)
    assert python == await over_asgi(url)


async def test_the_encoders_narrow_a_projection_in_a_field(url: str) -> None:
    database = await Database.connect(url)
    with use_database(database):
        held: Any = One(item=await Gadget.filter(id=1).aget_as(GPriv))
        written = core.encode_json(held, None, False, None)
        assert json.loads(written) == {"item": ONE}
        assert held.dump() == {"item": ONE}
        exact = One(item=await Gadget.filter(id=1).aget_as(GPub))
        assert core.encode_json(exact, None, False, None) == written
    database.close()
