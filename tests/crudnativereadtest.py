"""Native generated-CRUD reads against the forced Python lane on PostgreSQL."""

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from hinoki import CrudAPI, Hinoki, Schema, core
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, QuerySet, field
from hinoki.runtime.rustserver import RustWorker
from hinoki.settings import AppSettings, CorsSettings, ServerSettings
from tests.crudplantest import PlanItems

DATABASE_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
ORIGIN = "https://client.example"
Response = tuple[int, list[tuple[str, str]], bytes]


class Gadget(Model, table="crud_parity_gadgets"):
    id: int = field(primary_key=True)
    name: str
    price: float
    active: bool
    seen_at: datetime
    note: str | None = None
    hidden: bool = False


class GadgetOut(Schema):
    id: int
    name: str
    price: float
    active: bool
    seen_at: datetime
    note: str | None


class Gadgets(CrudAPI, model=Gadget, path="/gadgets"):
    read_schema = GadgetOut
    response_headers = (("x-gadget", "1"),)


GADGET_TABLE = (
    "CREATE TABLE crud_parity_gadgets (id bigint PRIMARY KEY, name text NOT NULL, "
    "price double precision NOT NULL, active boolean NOT NULL, "
    "seen_at timestamptz NOT NULL, note text, hidden boolean NOT NULL)"
)
GADGET_ROWS = (
    (5, "five", 1.5, True, "2026-01-02T03:04:05.678901+00:00", None, False),
    (7, "seven", -0.25, False, "2026-03-04T05:06:07+00:00", "a note", False),
    (9, "nine", 2.0, True, "2026-05-06T07:08:09+00:00", "secret", True),
)
PLAN_ITEM_ID = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")


async def request(
    port: int, method: str, target: str, headers: Sequence[tuple[str, str]] = ()
) -> Response:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = "".join(f"{name}: {value}\r\n" for name, value in headers)
    writer.write(
        (
            f"{method} {target} HTTP/1.1\r\nhost: test\r\n"
            "x-request-id: 00000000000000000000000000000001\r\n"
            f"content-length: 0\r\n{extra}\r\n"
        ).encode("latin-1")
    )
    await writer.drain()
    block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = block.decode("latin-1").split("\r\n")
    pairs = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next((int(v) for n, v in pairs if n == "content-length"), 0)
    body = b"" if method == "HEAD" else await reader.readexactly(length)
    writer.close()
    await writer.wait_closed()
    return int(lines[0].split()[1]), pairs, body  # type: ignore[return-value]


def stable(response: Response) -> tuple[int, list[tuple[str, str]], bytes]:
    status, headers, body = response
    kept = [(n, v) for n, v in headers if n not in {"date", "x-request-id"}]
    return status, kept, body


async def run_sql(statements: Sequence[tuple[str, tuple[Any, ...]]]) -> None:
    assert DATABASE_URL is not None
    database = await Database.connect(DATABASE_URL, max_size=1, wait=1)
    for sql, values in statements:
        await database.execute(sql, *values)
    database.close()


async def create_gadgets() -> None:
    await run_sql(
        [("DROP TABLE IF EXISTS crud_parity_gadgets", ()), (GADGET_TABLE, ())]
    )
    for row in GADGET_ROWS:
        await run_sql(
            [
                (
                    "INSERT INTO crud_parity_gadgets VALUES "
                    "($1, $2, $3, $4, $5, $6, $7)",
                    (*row[:4], datetime.fromisoformat(row[4]), *row[5:]),
                )
            ]
        )


@pytest.fixture
async def gadget_worker() -> AsyncIterator[RustWorker]:
    assert DATABASE_URL is not None
    await create_gadgets()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(
                trusted_proxies=("127.0.0.1",), trust_request_id=True
            ),
            cors=CorsSettings(origins=(ORIGIN,)),
        ),
        include=[Gadgets],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = RustWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        yield worker
    finally:
        await worker.stop()
        await run_sql([("DROP TABLE IF EXISTS crud_parity_gadgets", ())])


def armed(worker: RustWorker) -> list[int]:
    found = [
        action
        for action, selection in enumerate(worker.pipeline.crud_plan_selections)
        if selection is not None
    ]
    assert found
    return found


def assert_armed(worker: RustWorker) -> None:
    assert all(worker.core._crud_armed(action) for action in armed(worker))


def assert_disarmed(worker: RustWorker) -> None:
    assert all(not worker.core._crud_armed(action) for action in armed(worker))


def forbid_python_reads(monkeypatch: pytest.MonkeyPatch, owner: type[CrudAPI]) -> None:
    """After this, only a native answer can succeed: the Python handler would
    call ``get_queryset`` and fail."""

    def refuse(*_args: object, **_kwargs: object) -> QuerySet[Model]:
        raise AssertionError("the Python lane served a native-only request")

    monkeypatch.setattr(owner, "get_queryset", refuse)


NATIVE_TARGETS = [
    ("GET", "/gadgets?page=1&page_size=2", ()),
    ("GET", "/gadgets?page=2&page_size=1", ()),
    ("GET", "/gadgets?page=9&page_size=1", ()),
    ("HEAD", "/gadgets", ()),
    ("GET", "/gadgets", (("origin", ORIGIN),)),
    ("GET", "/gadgets/5", ()),
    ("GET", "/gadgets/7?unused=1", ()),
    ("GET", "/gadgets/7?%C3%A9=1", ()),
    ("GET", "/gadgets/-3", ()),
    ("GET", "/gadgets/0005", ()),
    ("GET", "/gadgets/5", (("origin", ORIGIN),)),
    ("GET", "/gadgets/5", (("origin", "https://other.example"),)),
    ("GET", "/gadgets/404", ()),
]
DEFERRED_TARGETS = [
    ("GET", "/gadgets/+5", ()),
    ("GET", "/gadgets/5?%FF", ()),
    ("GET", "/gadgets/5?" + "&".join(f"k{i}=1" for i in range(1001)), ()),
    ("GET", "/gadgets/%35", ()),
    ("GET", "/gadgets/1e3", ()),
    ("GET", "/gadgets/5.0", ()),
    ("GET", "/gadgets/9999999999999999999", ()),
    ("GET", "/gadgets/%D9%A3", ()),
]


async def answers(worker: RustWorker, targets: list[tuple[str, str, tuple]]) -> list:  # type: ignore[type-arg]
    port = worker.address[1]
    return [stable(await request(port, *target)) for target in targets]


async def test_native_gadget_reads_match_the_python_lane(
    gadget_worker: RustWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = gadget_worker
    assert_armed(worker)
    monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
    with monkeypatch.context() as served:
        forbid_python_reads(served, Gadgets)
        native = await answers(worker, NATIVE_TARGETS)
    deferred = await answers(worker, DEFERRED_TARGETS)
    assert [item[0] for item in native[:3]] == [200, 200, 200]
    assert all(item[0] != 500 for item in native)
    worker.core._disarm_crud_plans()
    assert_disarmed(worker)
    python = await answers(worker, NATIVE_TARGETS)
    python_deferred = await answers(worker, DEFERRED_TARGETS)
    assert native == python
    assert deferred == python_deferred


async def test_native_gadget_answers_cover_the_column_kinds(
    gadget_worker: RustWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gadget_worker, "_check_crud_snapshots", lambda: None)
    forbid_python_reads(monkeypatch, Gadgets)
    first = await request(gadget_worker.address[1], "GET", "/gadgets/5")
    second = await request(gadget_worker.address[1], "GET", "/gadgets/7")
    assert core.decode_json(first[2]) == {
        "id": 5,
        "name": "five",
        "price": 1.5,
        "active": True,
        "seen_at": "2026-01-02T03:04:05.678901+00:00",
        "note": None,
    }
    assert core.decode_json(second[2])["note"] == "a note"
    assert dict(first[1])["x-gadget"] == "1"


async def test_replacing_model_all_after_start_disarms_the_plans(
    gadget_worker: RustWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = gadget_worker
    port = worker.address[1]
    assert_armed(worker)
    hidden = await request(port, "GET", "/gadgets/9")
    assert hidden[0] == 200

    def visible(cls: type[Model]) -> QuerySet[Model]:
        return QuerySet(cls).filter(hidden=False)

    monkeypatch.setattr(Gadget, "all", classmethod(visible))
    worker.housekeeping()

    assert_disarmed(worker)
    assert (await request(port, "GET", "/gadgets/9"))[0] == 404
    listing = core.decode_json((await request(port, "GET", "/gadgets"))[2])
    assert listing["total"] == 2


async def test_replacing_the_base_model_all_after_start_disarms_the_plans(
    gadget_worker: RustWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nothing(cls: type[Model]) -> QuerySet[Model]:
        return QuerySet(cls).filter(hidden=False)

    monkeypatch.setattr(Model, "all", classmethod(nothing))
    gadget_worker.housekeeping()
    assert_disarmed(gadget_worker)


async def test_changing_response_headers_after_start_disarms_the_plans(
    gadget_worker: RustWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = gadget_worker
    port = worker.address[1]
    assert dict((await request(port, "GET", "/gadgets/5"))[1])["x-gadget"] == "1"

    monkeypatch.setattr(Gadgets, "response_headers", (("x-gadget", "2"),))
    worker.housekeeping()

    assert_disarmed(worker)
    assert dict((await request(port, "GET", "/gadgets/5"))[1])["x-gadget"] == "2"


UUID_TARGETS = [
    f"/plan-items/{PLAN_ITEM_ID}",
    f"/plan-items/{str(PLAN_ITEM_ID).upper()}",
    f"/plan-items/{PLAN_ITEM_ID.hex}",
    f"/plan-items/{{{PLAN_ITEM_ID}}}",
    f"/plan-items/urn:uuid:{PLAN_ITEM_ID}",
    f"/plan-items/%7B{PLAN_ITEM_ID}%7D",
    "/plan-items/00000000-0000-0000-0000-000000000000",
    "/plan-items/not-a-uuid",
]


async def test_uuid_path_forms_match_the_python_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    await run_sql(
        [
            ("DROP TABLE IF EXISTS crud_plan_items", ()),
            (
                "CREATE TABLE crud_plan_items (id uuid PRIMARY KEY, "
                "name text NOT NULL, label text NOT NULL)",
                (),
            ),
            ("INSERT INTO crud_plan_items VALUES ($1, 'u', 'l')", (PLAN_ITEM_ID,)),
        ]
    )
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[PlanItems],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = RustWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
        targets = [("GET", target, ()) for target in UUID_TARGETS]
        with monkeypatch.context() as served:
            forbid_python_reads(served, PlanItems)
            fast = await answers(worker, targets[:2])
        native = fast + await answers(worker, targets[2:])
        worker.core._disarm_crud_plans()
        python = await answers(worker, targets)
        assert native == python
        assert [item[0] for item in python] == [200, 200, 422, 422, 422, 422, 404, 422]
    finally:
        await worker.stop()
        await run_sql([("DROP TABLE IF EXISTS crud_plan_items", ())])
