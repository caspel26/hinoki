"""A ``bytes`` model column through generated CRUD on the Rust server, per
backend: base64 on the wire, raw bytes in the row."""

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from hinoki import API, Hinoki, Schema, crud, derive
from hinoki.db import Database, DatabaseModule
from hinoki.fieldtypes import MAX_BYTES_LENGTH
from hinoki.orm import Model, field
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, ServerSettings
from tests.crudnativewritetest import masked, request

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")


class Blob(Model, table="scalar_crud_blobs"):
    id: int = field(primary_key=True)
    name: str
    content: bytes
    preview: bytes | None = None


@derive(Blob)
class BlobOut(Schema):
    pass


@derive(Blob)
class BlobIn(Schema):
    pass


@crud(Blob, path="/blobs", read=BlobOut, create=BlobIn, public_writes=True)
class Blobs(API):
    pass


DATABASES = (
    pytest.param(("sqlite", None), id="sqlite"),
    pytest.param(
        ("postgres", POSTGRES_URL),
        marks=pytest.mark.skipif(
            POSTGRES_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
        ),
        id="postgresql",
    ),
)


@pytest.fixture(params=DATABASES)
async def worker(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[KisoWorker, Database]]:
    dialect, url = request.param
    url = url or f"sqlite:///{tmp_path / 'blobs.db'}"
    database = await Database.connect(url, max_size=1, wait=1)
    await database.execute(f"DROP TABLE IF EXISTS {Blob.__table__}")
    await database.execute(create_table_sql(Blob.__table__, table_state(Blob), dialect))
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Blobs],
        modules=[DatabaseModule(url)],
    )
    started = KisoWorker(app, "127.0.0.1", 0)
    await started.start()
    try:
        yield started, database
    finally:
        await started.stop()
        await database.execute(f"DROP TABLE IF EXISTS {Blob.__table__}")
        database.close()


async def test_bytes_are_written_and_read_as_base64(
    worker: tuple[KisoWorker, Database],
) -> None:
    started, database = worker
    port = started.address[1]
    created = masked(
        await request(
            port,
            "POST",
            "/blobs",
            b'{"id": 1, "name": "a", "content": "AAH+/w==", "preview": null}',
        )
    )
    assert created[0] == 201, created
    assert created[2]["content"] == "AAH+/w=="
    stored = await database.fetch_one(
        f"SELECT content, preview FROM {Blob.__table__} WHERE id = 1"  # noqa: S608 - fixed test table
    )
    assert stored is not None
    assert bytes(stored["content"]) == b"\x00\x01\xfe\xff"
    assert stored["preview"] is None
    refused = masked(
        await request(
            port, "POST", "/blobs", b'{"id": 2, "name": "b", "content": "AA"}'
        )
    )
    assert refused[0] == 422
    assert [(d["path"], d["code"]) for d in refused[2]["details"]] == [
        ("data.content", "bytes")
    ]
    one = masked(await request(port, "GET", "/blobs/1"))
    assert one[0] == 200
    assert one[2]["content"] == "AAH+/w=="


async def test_a_stored_row_beyond_the_wire_default_reads_back(
    worker: tuple[KisoWorker, Database],
) -> None:
    started, database = worker
    big = b"\x07" * (MAX_BYTES_LENGTH + 1)
    placeholder = "$1" if database.dialect == "postgres" else "?"
    await database.execute(
        f"INSERT INTO {Blob.__table__} (id, name, content) "  # noqa: S608 - fixed test table
        f"VALUES (5, 'big', {placeholder})",
        big,
    )
    port = started.address[1]
    one = masked(await request(port, "GET", "/blobs/5"))
    assert one[0] == 200
    assert base64.b64decode(one[2]["content"]) == big
    page = masked(await request(port, "GET", "/blobs"))
    assert page[0] == 200
    assert base64.b64decode(page[2]["items"][0]["content"]) == big
