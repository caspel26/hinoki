"""Native generated-CRUD writes against the forced Python lane, per backend."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import CrudAPI, Hinoki, Schema, Unset, core
from hinoki.db import Database, DatabaseModule
from hinoki.fields import UNSET
from hinoki.orm import Model, field, timestamps
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.runtime.rustserver import RustWorker
from hinoki.settings import AppSettings, ServerSettings
from hinoki.utils import utc_now

POSTGRES_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")
Response = tuple[int, list[tuple[str, str]], bytes]


@timestamps()
class Part(Model, table="crud_parity_parts"):
    id: int = field(primary_key=True)
    name: str
    stock: int = 0
    updated_at: datetime = field(default_factory=utc_now)


class PartOut(Schema):
    id: int
    name: str
    stock: int
    updated_at: datetime


class PartIn(Schema):
    id: int
    name: str
    stock: int = 3


class PartPatch(Schema):
    name: str | Unset = UNSET
    stock: int | Unset = UNSET


class Parts(CrudAPI, model=Part, path="/parts"):
    read_schema = PartOut
    create_schema = PartIn
    update_schema = PartPatch
    deletable = True
    public_writes = True


class Score(Model, table="crud_parity_scores"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    score: float = 5.0
    count: int = 0


class ScoreOut(Schema):
    id: UUID
    name: str
    score: float
    count: int


class ScoreIn(Schema):
    name: str
    score: float = 1


class Scores(CrudAPI, model=Score, path="/scores"):
    read_schema = ScoreOut
    create_schema = ScoreIn
    deletable = True
    public_writes = True


DATABASES = (
    pytest.param(("sqlite", None), id="sqlite"),
    pytest.param(
        ("postgres", POSTGRES_URL),
        marks=pytest.mark.skipif(
            POSTGRES_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
        ),
        id="postgresql",
    ),
    pytest.param(
        ("mysql", MYSQL_URL),
        marks=pytest.mark.skipif(
            MYSQL_URL is None, reason="HINOKI_TEST_MYSQL_URL is not set"
        ),
        id="mysql",
    ),
    pytest.param(
        ("mariadb", MARIADB_URL),
        marks=pytest.mark.skipif(
            MARIADB_URL is None, reason="HINOKI_TEST_MARIADB_URL is not set"
        ),
        id="mariadb",
    ),
)
MODELS = (Part, Score)


async def request(port: int, method: str, target: str, body: bytes = b"") -> Response:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = (
        f"{method} {target} HTTP/1.1\r\nhost: test\r\n"
        "x-request-id: 00000000000000000000000000000001\r\n"
        f"content-type: application/json\r\ncontent-length: {len(body)}\r\n\r\n"
    )
    writer.write(head.encode("latin-1") + body)
    await writer.drain()
    block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = block.decode("latin-1").split("\r\n")
    pairs = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next((int(v) for n, v in pairs if n == "content-length"), 0)
    payload = await asyncio.wait_for(reader.readexactly(length), 5)
    writer.close()
    await writer.wait_closed()
    return int(lines[0].split()[1]), pairs, payload  # type: ignore[return-value]


def masked(response: Response) -> tuple[int, list[tuple[str, str]], Any]:
    status, headers, body = response
    kept = [
        (name, value)
        for name, value in headers
        if name not in {"date", "x-request-id", "content-length"}
    ]
    document = core.decode_json(body) if body else None
    return status, kept, mask_values(document)


def mask_values(value: Any) -> Any:
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            value = {**value, "items": sorted(value["items"], key=lambda i: i["name"])}
        return {
            key: "<masked>"
            if key == "updated_at" or (key == "id" and isinstance(item, str))
            else mask_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_values(item) for item in value]
    return value


async def reset(url: str, dialect: str) -> None:
    database = await Database.connect(url, max_size=1, wait=1)
    for model in MODELS:
        await database.execute(f"DROP TABLE IF EXISTS {model.__table__}")
        await database.execute(
            create_table_sql(model.__table__, table_state(model), dialect)
        )
    database.close()


async def empty(url: str) -> None:
    database = await Database.connect(url, max_size=1, wait=1)
    for model in MODELS:
        await database.execute(f"DELETE FROM {model.__table__}")  # noqa: S608 - fixed test tables
    database.close()


@pytest.fixture(params=DATABASES)
async def worker(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[RustWorker, str]]:
    dialect, url = request.param
    url = url or f"sqlite:///{tmp_path / 'parity.db'}"
    await reset(url, dialect)
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[Parts, Scores],
        modules=[DatabaseModule(url)],
    )
    started = RustWorker(app, "127.0.0.1", 0)
    await started.start()
    try:
        yield started, url
    finally:
        await started.stop()
        cleanup = await Database.connect(url, max_size=1, wait=1)
        for model in MODELS:
            await cleanup.execute(f"DROP TABLE IF EXISTS {model.__table__}")
        cleanup.close()


def selected(started: RustWorker, path: str) -> set[str]:
    return {
        selection.action
        for selection in started.pipeline.crud_plan_selections
        if selection is not None and selection.binding.route.path.startswith(path)
    }


PART_SEQUENCE = [
    ("POST", "/parts", b'{"id":1,"name":"bolt"}'),
    ("POST", "/parts", b'{"id":2,"name":"nut","stock":9}'),
    ("POST", "/parts", b'{"id":1,"name":"again"}'),
    ("POST", "/parts", b'{"name":"no id"}'),
    ("POST", "/parts?%FF=1", b'{"id":3,"name":"query"}'),
    ("PATCH", "/parts/1", b'{"name":"washer"}'),
    ("PATCH", "/parts/1", b"{}"),
    ("PATCH", "/parts/1", b'{"id":9}'),
    ("PATCH", "/parts/99", b'{"name":"missing"}'),
    ("PATCH", "/parts/1?%FF", b'{"name":"query"}'),
    ("DELETE", "/parts/99", b""),
    ("DELETE", "/parts/2?%FF", b""),
    ("DELETE", "/parts/2", b""),
    ("GET", "/parts", b""),
]


async def run(started: RustWorker, sequence: list[tuple[str, str, bytes]]) -> list:  # type: ignore[type-arg]
    port = started.address[1]
    return [masked(await request(port, *step)) for step in sequence]


async def test_int_key_timestamp_and_schema_default_writes_match_python(
    worker: tuple[RustWorker, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started, url = worker
    monkeypatch.setattr(started, "_check_crud_snapshots", lambda: None)
    assert selected(started, "/parts") >= {"create", "update", "delete"}
    originals = {
        name: getattr(Parts, name)
        for name in ("perform_create", "perform_update", "perform_delete")
    }
    for name in originals:
        monkeypatch.setattr(
            Parts,
            name,
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("native CRUD write called a Python hook")
            ),
        )
    native = await run(started, PART_SEQUENCE)
    started.core._disarm_crud_plans()
    for name, value in originals.items():
        monkeypatch.setattr(Parts, name, value)
    await empty(url)
    python = await run(started, PART_SEQUENCE)
    assert native == python
    statuses = [item[0] for item in native]
    assert statuses[:2] == [201, 201]
    assert 404 in statuses
    assert 422 in statuses
    assert 400 in statuses
    assert native[0][2]["stock"] == 3


async def test_schema_defaults_of_another_kind_than_the_column_match_python(
    worker: tuple[RustWorker, str],
) -> None:
    started, url = worker
    assert not selected(started, "/scores")
    sequence = [
        ("POST", "/scores", b'{"name":"a"}'),
        ("POST", "/scores", b'{"name":"b","score":2.5}'),
        ("GET", "/scores?page_size=100", b""),
    ]
    native = await run(started, sequence)
    started.core._disarm_crud_plans()
    await empty(url)
    python = await run(started, sequence)
    assert native == python
    assert native[0][2]["score"] == 1.0
    assert native[0][2]["count"] == 0
