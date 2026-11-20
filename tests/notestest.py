"""The notes example end to end.

``tests/conftest.py`` points ``NOTES_DATABASE__URL`` at the test database before
this module imports the application (the pattern of docs/testing.md).
"""

import os
from collections.abc import AsyncIterator

import pytest
from notes.main import app
from notes.settings import NotesSettings

from hinoki.db import Database
from tests.harness import Client, Pool

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(URL is None, reason="no database")
JSON = "application/json"


def test_settings_default_to_local_sqlite() -> None:
    settings = NotesSettings.load(env={})
    assert settings.database.url.reveal() == "sqlite://./.hinoki/local.sqlite3"


@pytest.fixture
async def client(pool: Pool) -> AsyncIterator[Client]:
    assert URL is not None
    db = await Database.connect(URL, max_size=1)
    await db.execute("DROP TABLE IF EXISTS notes")
    await db.execute(
        "CREATE TABLE notes (id uuid PRIMARY KEY, title varchar(80) NOT NULL, "
        "body text NOT NULL)"
    )
    yield await pool.start(app)
    await db.execute("DROP TABLE IF EXISTS notes")
    db.close()


async def test_the_whole_api_in_one_file(client: Client) -> None:
    made = await client.request(
        "POST", "/notes", b'{"title": "Hi", "body": "there"}', content_type=JSON
    )
    assert made.status == 201 and made.body["title"] == "Hi"
    padded = await client.request(
        "POST", "/notes", b'{"title": "  Padded  "}', content_type=JSON
    )
    assert padded.body["title"] == "Padded"  # the model's before_save hook
    await client.request("DELETE", f"/notes/{padded.body['id']}")
    key = made.body["id"]
    assert (await client.request("GET", f"/notes/{key}")).body == made.body
    assert [n["title"] for n in (await client.request("GET", "/notes")).body] == ["Hi"]
    assert (
        await client.request("POST", "/notes", b'{"title": ""}', content_type=JSON)
    ).status == 422
    assert (await client.request("DELETE", f"/notes/{key}")).status == 204
    assert (await client.request("GET", f"/notes/{key}")).status == 404
    assert (await client.request("GET", "/docs")).status == 200
