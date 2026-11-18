"""Model hooks through generated CRUD, and the native lane they leave."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, Schema, Unset, http
from hinoki.crud import CrudAPI
from hinoki.db import Database, DatabaseModule
from hinoki.fields import UNSET
from hinoki.orm import Model, pk
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import build_server_core
from hinoki.testing import ASGIClient
from tests.harness import Client, Pool

JSON = "application/json"
SEEN: list[str] = []


class Post(Model, table="hooked_posts"):
    id: int = pk()
    title: str
    slug: str = ""

    async def before_save(self) -> None:
        SEEN.append(f"before_save {self.title}")
        self.slug = self.title.lower()

    async def after_save(self) -> None:
        SEEN.append(f"after_save {sorted(self.changes)}")


class PostOut(Schema):
    id: int
    title: str
    slug: str


class PostIn(Schema):
    id: int
    title: str


class PostPatch(Schema):
    title: str | Unset = UNSET


class Posts(CrudAPI, model=Post, path="/posts"):
    read_schema = PostOut
    create_schema = PostIn
    update_schema = PostPatch
    deletable = True
    public_writes = True


def body(**values: Any) -> bytes:
    return json.dumps(values).encode()


@pytest.fixture
async def client(pool: Pool, tmp_path: Path) -> AsyncIterator[Client]:
    SEEN.clear()
    url = f"sqlite://{tmp_path / 'posts.sqlite3'}"
    admin = await Database.connect(url, max_size=1)
    await admin.execute(
        "CREATE TABLE hooked_posts "
        "(id integer PRIMARY KEY, title text NOT NULL, slug text NOT NULL)"
    )
    app = Hinoki(include=[Posts], modules=[DatabaseModule(url, max_size=2)])
    yield await pool.start(app)
    admin.close()


async def test_generated_create_and_update_run_the_hooks(client: Client) -> None:
    created = await client.request(
        "POST", "/posts", body(id=1, title="Hello"), content_type=JSON
    )
    assert created.status == 201
    assert created.body["slug"] == "hello"
    assert SEEN[0] == "before_save Hello"

    SEEN.clear()
    updated = await client.request(
        "PATCH", "/posts/1", body(title="Bye"), content_type=JSON
    )
    assert updated.status == 200
    assert updated.body["slug"] == "bye"
    assert SEEN == ["before_save Bye", "after_save ['slug', 'title']"]


def test_hooked_models_keep_native_reads_and_lose_native_writes() -> None:
    app = Hinoki(
        include=[Posts], modules=[DatabaseModule("sqlite:///hooks-unused.sqlite3")]
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)

    by_route = {
        (binding.route.method, binding.route.path): reason
        for binding, reason in zip(
            pipeline.bindings, pipeline.crud_plan_reasons, strict=True
        )
    }
    assert by_route[("GET", "/posts")] == "eligible"
    assert by_route[("GET", "/posts/{item_id}")] == "eligible"
    assert by_route[("DELETE", "/posts/{item_id}")] == "eligible"
    assert by_route[("POST", "/posts")] == "custom_or_unsupported_model"
    assert by_route[("PATCH", "/posts/{item_id}")] == "custom_or_unsupported_model"


class Doc(Model, table="scoped_docs"):
    id: int = pk()
    title: str
    tenant: str = "a"

    async def before_save(self) -> None:
        # another request moves the row out of this tenant before our UPDATE runs
        if self.title == "move-me":
            await Doc.filter(id=self.id).aupdate({"tenant": "b"})


class DocOut(Schema):
    id: int
    title: str
    tenant: str


class DocIn(Schema):
    id: int
    title: str


class DocPatch(Schema):
    title: str | Unset = UNSET


class Docs(CrudAPI, model=Doc, path="/docs"):
    read_schema = DocOut
    create_schema = DocIn
    update_schema = DocPatch
    public_writes = True

    def get_queryset(self, principal: object) -> Any:
        return Doc.filter(tenant="a")


class Thing(Model, table="sync_things"):
    id: int = pk()
    name: str

    def before_save(self) -> None:
        self.name = self.name.upper()


async def test_a_hooked_update_cannot_write_a_row_that_left_the_scope(
    pool: Pool, tmp_path: Path
) -> None:
    url = f"sqlite://{tmp_path / 'docs.sqlite3'}"
    admin = await Database.connect(url, max_size=1)
    await admin.execute(
        "CREATE TABLE scoped_docs (id integer PRIMARY KEY, title text NOT NULL, "
        "tenant text NOT NULL)"
    )
    await admin.execute(
        "INSERT INTO scoped_docs VALUES (1, 'plain', 'a'), (2, 'other', 'a')"
    )
    app = Hinoki(include=[Docs], modules=[DatabaseModule(url, max_size=2)])
    client = await pool.start(app)

    fine = await client.request(
        "PATCH", "/docs/1", body(title="edited"), content_type=JSON
    )
    refused = await client.request(
        "PATCH", "/docs/2", body(title="move-me"), content_type=JSON
    )

    assert fine.status == 200 and fine.body["title"] == "edited"
    assert refused.status in {403, 404}
    rows = await admin.fetch("SELECT title, tenant FROM scoped_docs WHERE id = 2")
    assert [dict(r) for r in rows] == [{"title": "other", "tenant": "a"}]  # rolled back
    admin.close()


async def test_a_sync_handler_can_write_a_hooked_model_in_a_transaction(
    tmp_path: Path,
) -> None:
    url = f"sqlite://{tmp_path / 'things.sqlite3'}"
    admin = await Database.connect(url, max_size=1)
    await admin.execute(
        "CREATE TABLE sync_things (id integer PRIMARY KEY, name text NOT NULL)"
    )

    @http.post("/things", status=201, atomic=True)
    def add() -> dict[str, str]:
        Thing.create(id=1, name="quiet")
        return {"ok": "yes"}

    app = Hinoki(include=[add], modules=[DatabaseModule(url, max_size=2)])
    async with ASGIClient(app) as client:
        assert (await client.post("/things")).status == 201
    rows = await admin.fetch("SELECT name FROM sync_things")
    assert [r["name"] for r in rows] == ["QUIET"]
    admin.close()
