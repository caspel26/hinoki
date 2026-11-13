"""The reference blog end to end: models, auth, transactions, pagination, docs.

Needs a disposable PostgreSQL (HINOKI_TEST_DATABASE_URL); skipped otherwise.
"""

import json
import os
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from openapi_spec_validator import validate

from examples.blog.main import create_app
from examples.blog.models import Post, User
from hinoki.db import Database
from hinoki.orm import use_database
from tests.blogschema import drop, reset
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
JSON = "application/json"
EDITOR = {"authorization": "Bearer editor-token"}
OTHER = {"authorization": "Bearer other-token"}
READER = {"authorization": "Bearer reader-token"}


@pytest.fixture
async def admin() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    await reset(database)
    with use_database(database):
        await User.acreate(name="Ada", token="editor-token", is_editor=True)
        await User.acreate(name="Bob", token="other-token", is_editor=True)
        await User.acreate(name="Rae", token="reader-token")
        yield database
    await drop(database)
    database.close()


@pytest.fixture
async def blog(admin: Database, pool: Pool) -> Client:
    assert URL is not None
    return await pool.start(create_app(URL))


def draft(slug: str = "hello-world") -> bytes:
    return json.dumps({"title": "Hello", "slug": slug, "body": "text"}).encode()


async def create(blog: Client, slug: str = "hello-world") -> str:
    reply = await blog.request(
        "POST", "/posts", draft(slug), content_type=JSON, headers=EDITOR
    )
    assert reply.status == 201, reply.body
    return str(reply.body["id"])


async def test_health_and_openapi_are_public_and_valid(blog: Client) -> None:
    assert (await blog.request("GET", "/health")).body == {"status": "ok"}
    document = (await blog.request("GET", "/openapi.json")).body
    validate(document)
    assert "Page_PostOut" in document["components"]["schemas"]
    assert document["components"]["securitySchemes"]["TokenAuth"]["scheme"] == "bearer"


async def test_the_full_publishing_flow(blog: Client) -> None:
    post_id = await create(blog)
    assert (await blog.request("GET", f"/posts/{post_id}")).status == 404  # draft
    assert (await blog.request("GET", "/posts")).body["total"] == 0

    published = await blog.request("POST", f"/posts/{post_id}/publish", headers=EDITOR)
    assert published.status == 200
    assert published.body["published"] is True
    assert published.body["author_name"] == "Ada"

    assert (await blog.request("GET", f"/posts/{post_id}")).status == 200
    listing = (await blog.request("GET", "/posts")).body
    assert (listing["total"], [p["id"] for p in listing["items"]]) == (1, [post_id])

    again = await blog.request("POST", f"/posts/{post_id}/publish", headers=EDITOR)
    assert (again.status, again.body["error"]) == (409, "post_already_published")


async def test_authentication_and_authorization(blog: Client) -> None:
    anonymous = await blog.request("POST", "/posts", draft(), content_type=JSON)
    assert anonymous.status == 401
    forbidden = await blog.request(
        "POST", "/posts", draft(), content_type=JSON, headers=READER
    )
    assert forbidden.status == 403

    post_id = await create(blog)
    not_owner = await blog.request("POST", f"/posts/{post_id}/publish", headers=OTHER)
    assert not_owner.status == 403
    cannot_delete = await blog.request("DELETE", f"/posts/{post_id}", headers=OTHER)
    assert cannot_delete.status == 403
    assert (
        await blog.request("DELETE", f"/posts/{post_id}", headers=EDITOR)
    ).status == 204
    assert await Post.all().acount() == 0


async def test_unique_slug_is_a_conflict_and_leaves_no_partial_write(
    blog: Client,
) -> None:
    await create(blog, "same-slug")
    reply = await blog.request(
        "POST", "/posts", draft("same-slug"), content_type=JSON, headers=EDITOR
    )
    assert (reply.status, reply.body["error"]) == (409, "unique_conflict")
    assert await Post.all().acount() == 1


async def test_validation_errors_are_reported_with_paths(blog: Client) -> None:
    bad = json.dumps({"title": "", "slug": "Not A Slug", "extra": 1}).encode()
    reply = await blog.request("POST", "/posts", bad, content_type=JSON, headers=EDITOR)
    assert reply.status == 422
    found = {d["path"]: d["code"] for d in reply.body["details"]}
    assert found == {
        "data.title": "min_length",
        "data.slug": "pattern",
        "data.body": "required",
        "data.extra": "extra_forbidden",
    }
    assert await Post.all().acount() == 0


async def test_listing_is_paginated_newest_first(blog: Client, admin: Database) -> None:
    author = await User.aget(token="editor-token")
    for n in range(12):
        await Post.acreate(
            author_id=author.id, title=f"t{n}", slug=f"s{n}", body="b", published=n < 11
        )
    first = (await blog.request("GET", "/posts", query="page_size=5")).body
    assert (first["total"], first["pages"], len(first["items"])) == (11, 3, 5)
    third = (await blog.request("GET", "/posts", query="page=3&page_size=5")).body
    assert len(third["items"]) == 1
    created = [p["created_at"] for p in first["items"]]
    assert created == sorted(created, reverse=True)
    too_big = await blog.request("GET", "/posts", query="page_size=500")
    assert too_big.status == 422
    UUID(first["items"][0]["id"])
