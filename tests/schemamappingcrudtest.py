"""``dict[str, X]`` fields through generated CRUD on the Rust server, per backend."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import pytest

from hinoki import API, Field, Hinoki, Schema, crud, derive
from hinoki.db import Database, DatabaseModule
from hinoki.fieldtypes import MAX_MAPPING_ITEMS
from hinoki.orm import Model, field
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, ServerSettings
from tests.crudnativewritetest import masked, request, selected

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")


class Setting(Model, table="mapping_crud_settings"):
    id: int = field(primary_key=True)
    name: str
    payload: dict[str, int]


@derive(Setting)
class SettingOut(Schema):
    pass


class SettingIn(Schema):
    id: int
    name: Annotated[str, Field(max_length=20)]
    payload: dict[str, int]


@crud(Setting, path="/settings", read=SettingOut, create=SettingIn, public_writes=True)
class Settings(API):
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
    url = url or f"sqlite:///{tmp_path / 'settings.db'}"
    database = await Database.connect(url, max_size=1, wait=1)
    await database.execute(f"DROP TABLE IF EXISTS {Setting.__table__}")
    await database.execute(
        create_table_sql(Setting.__table__, table_state(Setting), dialect)
    )
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Settings],
        modules=[DatabaseModule(url)],
    )
    started = KisoWorker(app, "127.0.0.1", 0)
    await started.start()
    try:
        yield started, database
    finally:
        await started.stop()
        await database.execute(f"DROP TABLE IF EXISTS {Setting.__table__}")
        database.close()


async def test_a_stored_row_beyond_the_input_bounds_reads_back(
    worker: tuple[KisoWorker, Database],
) -> None:
    started, database = worker
    payload = {f"{'k' * 300}{index}": index for index in range(MAX_MAPPING_ITEMS + 1)}
    text = json.dumps(payload).replace("'", "''")
    await database.execute(
        f"INSERT INTO {Setting.__table__} (id, name, payload) "  # noqa: S608 - fixed test table
        f"VALUES (1, 'big', '{text}')"
    )
    port = started.address[1]
    one = masked(await request(port, "GET", "/settings/1"))
    assert one[0] == 200
    assert one[2]["payload"] == payload
    page = masked(await request(port, "GET", "/settings"))
    assert page[0] == 200
    assert page[2]["items"][0]["payload"] == payload


async def test_dict_writes_match_the_python_lane(
    worker: tuple[KisoWorker, Database],
) -> None:
    started, database = worker
    big = json.dumps({f"k{i}": i for i in range(MAX_MAPPING_ITEMS + 1)}).encode()
    sequence = [
        ("POST", "/settings", b'{"id": 1, "name": "a", "payload": {"b": 2, "a": 1}}'),
        ("POST", "/settings", b'{"id": 2, "name": "b", "payload": {"x": "1"}}'),
        ("POST", "/settings", b'{"id": 3, "name": "c", "payload": ' + big + b"}"),
        ("POST", "/settings", b'{"id": 4, "name": "d", "payload": {"a": 1, "a": 5}}'),
        ("GET", "/settings/1", b""),
        ("GET", "/settings/4", b""),
    ]
    # A dict column is a JSON column, which the native write plan does not
    # carry: the write runs in Python on both passes and must agree.
    assert "create" not in selected(started, "/settings")
    port = started.address[1]
    native = [masked(await request(port, *step)) for step in sequence]
    started.core._disarm_crud_plans()
    await database.execute(f"DELETE FROM {Setting.__table__}")  # noqa: S608 - fixed test table
    python = [masked(await request(port, *step)) for step in sequence]
    assert native == python
    assert [item[0] for item in native] == [201, 422, 422, 201, 200, 200]
    assert native[4][2]["payload"] == {"b": 2, "a": 1}  # jsonb does not keep order
    assert native[5][2]["payload"] == {"a": 5}
    assert [d["path"] for d in native[1][2]["details"]] == ["data.payload.x"]
    assert [d["code"] for d in native[2][2]["details"]] == ["max_length"]
