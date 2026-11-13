"""The notes example end to end (PostgreSQL)."""

import os
from collections.abc import AsyncIterator

import pytest

from examples.notes.app import create_app
from hinoki.db import Database
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(URL is None, reason="no database")
JSON = "application/json"


@pytest.fixture
async def client(pool: Pool) -> AsyncIterator[Client]:
    assert URL is not None
    db = await Database.connect(URL, max_size=1)
    await db.execute("DROP TABLE IF EXISTS notes")
    await db.execute(
        "CREATE TABLE notes (id uuid PRIMARY KEY, title varchar(80) NOT NULL, "
        "body text NOT NULL)"
    )
    yield await pool.start(create_app(URL))
    await db.execute("DROP TABLE IF EXISTS notes")
    db.close()


async def test_the_whole_api_in_one_file(client: Client) -> None:
    made = await client.request(
        "POST", "/notes", b'{"title": "Hi", "body": "there"}', content_type=JSON
    )
    assert made.status == 201 and made.body["title"] == "Hi"
    key = made.body["id"]
    assert (await client.request("GET", f"/notes/{key}")).body == made.body
    assert [n["title"] for n in (await client.request("GET", "/notes")).body] == ["Hi"]
    assert (
        await client.request("POST", "/notes", b'{"title": ""}', content_type=JSON)
    ).status == 422
    assert (await client.request("DELETE", f"/notes/{key}")).status == 204
    assert (await client.request("GET", f"/notes/{key}")).status == 404
    assert (await client.request("GET", "/docs")).status == 200
