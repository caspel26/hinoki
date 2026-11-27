"""Regression tests for CRUD list/retrieve Rust projection and custom hooks."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

from hinoki import API, Access, Hinoki, RowScope, RuntimeConfig, Schema, crud
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.db import Database, DatabaseModule
from hinoki.modules import Module, ModuleContext
from hinoki.orm import Model, field
from hinoki.testing import ASGIClient


class ProjItem(Model, table="proj_crud_items"):
    id: int = field(primary_key=True)
    tenant_id: str
    title: str


class ProjItemOut(Schema):
    id: int
    tenant_id: str
    title: str


class _Seed(Module):
    name = "proj-crud-seed"
    requires = (DatabaseModule,)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        db = ctx.get(Database)
        await db.execute(
            "create table if not exists proj_crud_items "
            "(id integer primary key, tenant_id text not null, title text not null)"
        )
        await db.execute(
            "insert or replace into proj_crud_items (id, tenant_id, title) "
            "values (?, ?, ?), (?, ?, ?), (?, ?, ?)",
            1,
            "t-a",
            "alpha",
            2,
            "t-a",
            "beta",
            3,
            "t-b",
            "gamma",
        )
        yield


def _make_auth() -> tuple[ClaimsJWTAuth, str]:
    key = JWTKey("k1", "HS256", os.urandom(32))

    class Tokens(ClaimsJWTAuth):
        keys: ClassVar[tuple[JWTKey, ...]] = (key,)

    auth = Tokens()
    token = auth.issue({"sub": "u1", "tenant_id": "t-a"}, ttl=300)
    return auth, f"Bearer {token}"


async def test_default_crud_list_and_retrieve_project_with_row_scope(
    tmp_path: Path,
) -> None:
    auth, bearer = _make_auth()
    resource = crud(
        ProjItem,
        path="/items",
        read=ProjItemOut,
        access=Access(auth=auth),
        scope=RowScope.by_claim(ProjItem, "tenant_id", "tenant_id"),
    )
    app = Hinoki(
        runtime=RuntimeConfig(),
        include=[resource],
        modules=[DatabaseModule(f"sqlite://{tmp_path / 'db.sqlite3'}"), _Seed()],
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    headers = {"authorization": bearer}
    async with ASGIClient(app) as client:
        listed = await client.get(
            "/items", query="page=1&page_size=10", headers=headers
        )
        assert listed.status == 200
        body = listed.body
        assert body["total"] == 2
        assert [row["title"] for row in body["items"]] == ["alpha", "beta"]

        found = await client.get("/items/1", headers=headers)
        assert found.status == 200
        assert found.body == {"id": 1, "tenant_id": "t-a", "title": "alpha"}

        hidden = await client.get("/items/3", headers=headers)
        assert hidden.status == 404


async def test_custom_to_schema_hook_is_not_bypassed(tmp_path: Path) -> None:
    auth, bearer = _make_auth()
    calls: list[int] = []

    @crud(
        ProjItem,
        path="/custom-items",
        read=ProjItemOut,
        access=Access(auth=auth),
        scope=RowScope.by_claim(ProjItem, "tenant_id", "tenant_id"),
    )
    class CustomItemsCrud(API):
        def to_schema(self, item: Any) -> ProjItemOut:
            calls.append(item.id)
            return ProjItemOut(
                id=item.id,
                tenant_id=item.tenant_id,
                title=f"hook:{item.title}",
            )

    app = Hinoki(
        runtime=RuntimeConfig(),
        include=[CustomItemsCrud],
        modules=[DatabaseModule(f"sqlite://{tmp_path / 'db.sqlite3'}"), _Seed()],
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    headers = {"authorization": bearer}
    async with ASGIClient(app) as client:
        listed = await client.get(
            "/custom-items", query="page=1&page_size=10", headers=headers
        )
        assert listed.status == 200
        assert [row["title"] for row in listed.body["items"]] == [
            "hook:alpha",
            "hook:beta",
        ]

        found = await client.get("/custom-items/1", headers=headers)
        assert found.status == 200
        assert found.body["title"] == "hook:alpha"

    assert calls == [1, 2, 1]
