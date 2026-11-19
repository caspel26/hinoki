"""``@native`` handlers: the marker, its registration checks, and database calls
made while Rust drives a coroutine."""

import asyncio
from pathlib import Path

import pytest

from hinoki import Hinoki, core, http, native
from hinoki.db import Database
from hinoki.errors import RegistrationError
from hinoki.http import Request
from hinoki.schema import Schema


class NoteOut(Schema):
    id: int


def test_the_marker_returns_the_function_and_composes_in_either_order() -> None:
    @http.get("/a/{id}")
    @native
    async def above(id: int) -> NoteOut:
        return NoteOut(id=id)

    @native
    @http.get("/b/{id}")
    async def below(id: int) -> NoteOut:
        return NoteOut(id=id)

    app = Hinoki()
    app.include(above, below)
    assert [r.path for r in app.routes] == ["/a/{id}", "/b/{id}"]


def test_a_sync_marked_handler_fails_at_registration() -> None:
    @http.get("/x")
    @native
    def handler() -> NoteOut:
        return NoteOut(id=1)

    with pytest.raises(RegistrationError, match="async def"):
        Hinoki().include(handler)


def test_a_marked_handler_taking_request_fails_at_registration() -> None:
    @http.get("/x")
    @native
    async def handler(request: Request) -> NoteOut:
        return NoteOut(id=1)

    with pytest.raises(RegistrationError, match="takes Request"):
        Hinoki().include(handler)


def test_an_unmarked_handler_is_never_checked() -> None:
    @http.get("/x")
    async def handler(request: Request) -> NoteOut:
        return NoteOut(id=1)

    Hinoki().include(handler)


async def test_database_calls_in_a_driven_coroutine_are_native_waits(
    tmp_path: Path,
) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'driven.sqlite3'}", max_size=2)
    await db.execute("create table t (id integer primary key, v text)")
    await db.execute("insert into t (v) values (?)", "one")

    async def handler() -> object:
        async with db.atomic():
            await db.execute("insert into t (v) values (?)", "two")
            return await db.fetch("select v from t order by id")

    rows = await asyncio.to_thread(core._drive, handler())
    assert [row["v"] for row in rows] == ["one", "two"]
    assert not core.driving()
    assert len(await db.fetch("select v from t")) == 2


async def test_a_driven_rollback_discards_the_write(tmp_path: Path) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'rollback.sqlite3'}")
    await db.execute("create table t (id integer primary key, v text)")

    async def handler() -> None:
        async with db.atomic():
            await db.execute("insert into t (v) values (?)", "x")
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await asyncio.to_thread(core._drive, handler())
    assert await db.fetch("select v from t") == []
