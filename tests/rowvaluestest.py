"""UUIDs and datetimes load and serialize with the values Python gives them."""

import copy
import json
import os
import pickle
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import Schema, core
from hinoki.db import Database

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)
SAMPLE = "12345678-9abc-def0-1234-56789abcdef0"
WHEN = datetime(2026, 11, 15, 10, 30, tzinfo=UTC)


class Inner(Schema):
    id: UUID
    name: str


class Outer(Schema):
    id: UUID
    when: datetime
    flag: bool
    count: int
    ratio: float
    note: str | None
    tags: list[str]
    inner: Inner


def outer() -> Outer:
    return Outer(
        id=UUID(SAMPLE),
        when=WHEN,
        flag=True,
        count=3,
        ratio=0.5,
        note=None,
        tags=["a", "b"],
        inner=Inner(id=UUID(int=1), name="n"),
    )


EXPECTED: dict[str, Any] = {
    "id": SAMPLE,
    "when": WHEN.isoformat(),
    "flag": True,
    "count": 3,
    "ratio": 0.5,
    "note": None,
    "tags": ["a", "b"],
    "inner": {"id": "00000000-0000-0000-0000-000000000001", "name": "n"},
}


def test_dump_gives_json_values_for_every_field_type() -> None:
    dumped = outer().dump()

    assert dumped == EXPECTED
    assert type(dumped["flag"]) is bool
    assert type(dumped["count"]) is int


def test_dump_keeps_python_values_when_not_serializing_to_json() -> None:
    plain = core.dump_instance(outer(), False)

    assert plain["id"] == UUID(SAMPLE)
    assert plain["when"] == WHEN
    assert plain["inner"]["id"] == UUID(int=1)


def test_encode_json_matches_dump() -> None:
    assert json.loads(core.encode_json(outer())) == EXPECTED


def test_a_str_subclass_with_dump_is_still_replaced_by_its_dump() -> None:
    class Marked(str):
        def dump(self) -> str:
            return "dumped"

    class Holder(Schema):
        value: str

    holder = Holder(value="x")
    holder.__dict__["value"] = Marked("raw")

    assert core.dump_instance(holder, True) == {"value": "dumped"}


def test_a_uuid_subclass_keeps_its_own_text() -> None:
    class Shouting(UUID):
        def __str__(self) -> str:
            return "LOUD"

    assert core.encode_json({"id": Shouting(SAMPLE)}) == b'{"id":"LOUD"}'


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2, wait=0.5)
    yield database
    database.close()


@needs_database
async def test_a_uuid_read_from_the_database_behaves_like_a_python_uuid(
    db: Database,
) -> None:
    rows = await db.fetch(f"SELECT '{SAMPLE}'::uuid AS id")
    loaded = rows[0]["id"]
    expected = UUID(SAMPLE)

    assert type(loaded) is UUID
    assert loaded == expected
    assert hash(loaded) == hash(expected)
    assert str(loaded) == SAMPLE
    assert loaded.int == expected.int
    assert loaded.hex == expected.hex
    assert loaded.bytes == expected.bytes
    assert loaded.version == expected.version
    assert loaded.is_safe == expected.is_safe
    assert {loaded: 1}[expected] == 1
    assert copy.deepcopy(loaded) == expected
    assert pickle.loads(pickle.dumps(loaded)) == expected  # noqa: S301 - own bytes
    with pytest.raises(TypeError):
        loaded.int = 5  # type: ignore[misc]


@needs_database
async def test_uuid_parameters_round_trip_through_the_database(db: Database) -> None:
    sent = uuid4()
    rows = await db.fetch("SELECT $1::uuid AS id, $1::uuid::text AS text", sent)

    assert rows[0]["id"] == sent
    assert rows[0]["text"] == str(sent)


def test_dump_leaves_a_uuid_subclass_as_it_was() -> None:
    """Only the exact UUID class is turned into text, as before the native change."""

    class Marked(UUID):
        pass

    class Holder(Schema):
        id: UUID

    holder = Holder(id=Marked(SAMPLE))
    dumped = core.dump_instance(holder, True)

    assert type(dumped["id"]) is Marked


@needs_database
async def test_database_uuids_are_built_without_running_uuid_init(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db.fetch("SELECT gen_random_uuid() AS id")  # the one-time self-check
    calls: list[int] = []
    original = UUID.__init__

    def counting(self: UUID, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        original(self, *args, **kwargs)

    monkeypatch.setattr(UUID, "__init__", counting)
    rows = await db.fetch("SELECT gen_random_uuid() AS a, gen_random_uuid() AS b")

    assert type(rows[0]["a"]) is UUID
    assert calls == []
