"""Typed events: registration, phases, isolation, limits and the request wiring."""

import asyncio
import logging
from argparse import Namespace
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from hinoki import Hinoki, events, http
from hinoki.db import Database
from hinoki.errors import EventError, EventRegistrationError, EventTimeout
from hinoki.management import Command, CommandContext
from hinoki.modules import ModuleContext
from hinoki.orm import use_database
from hinoki.runtime.management import run_command
from hinoki.settings import EventsSettings
from hinoki.testing import ASGIClient


@dataclass(frozen=True)
class Noted:
    text: str


@dataclass(frozen=True)
class About[S]:
    subject: type[S]


class Note: ...


class Other: ...


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    opened = await Database.connect(
        f"sqlite://{tmp_path / 'events.sqlite3'}", max_size=2
    )
    await opened.execute("CREATE TABLE seen (name text)")
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


def bus_of(*collections: events.Handlers, **limits: float) -> events.EventBus:
    return events.EventBus(collections, EventsSettings(**limits))  # type: ignore[arg-type]


async def test_handlers_run_in_registration_order_across_collections() -> None:
    seen: list[str] = []
    first, second = events.Handlers(), events.Handlers()

    @first.after_commit(Noted)
    async def one(event: Noted) -> None:
        seen.append("one")

    @second.after_commit(Noted)
    async def two(event: Noted) -> None:
        seen.append("two")

    @first.after_commit(Noted)
    async def three(event: Noted) -> None:
        seen.append("three")

    bus = bus_of(first, second)
    await bus.emit(Noted("x"))
    assert seen == ["one", "three", "two"]
    assert [r.name.rsplit(".", 1)[1] for r in bus.registrations] == [
        "one",
        "three",
        "two",
    ]


def test_registration_refuses_untyped_mistyped_and_misplaced_handlers() -> None:
    handlers = events.Handlers()

    async def untyped(event) -> None: ...  # type: ignore[no-untyped-def]

    async def wrong(event: About[Note]) -> None: ...

    def sync_inside(event: Noted) -> None: ...

    async def two_params(event: Noted, extra: int) -> None: ...

    with pytest.raises(EventRegistrationError, match="has no type"):
        handlers.after_commit(Noted)(untyped)
    with pytest.raises(EventRegistrationError, match="registered for Noted"):
        handlers.after_commit(Noted)(wrong)  # type: ignore[arg-type]
    with pytest.raises(EventRegistrationError, match="must be 'async def'"):
        handlers.in_transaction(Noted)(sync_inside)  # type: ignore[arg-type]
    with pytest.raises(EventRegistrationError, match="exactly one"):
        handlers.after_commit(Noted)(two_params)  # type: ignore[arg-type]
    with pytest.raises(EventRegistrationError, match="not an event class"):
        handlers.after_commit(3)  # type: ignore[arg-type]
    assert handlers.registrations == ()


def test_bus_refuses_foreign_duplicate_and_excess_registrations() -> None:
    handlers = events.Handlers()
    with pytest.raises(EventRegistrationError, match="Handlers collections"):
        events.EventBus([object()])  # type: ignore[list-item]
    with pytest.raises(EventRegistrationError, match="listed twice"):
        events.EventBus([handlers, handlers])

    @handlers.after_commit(Noted)
    async def one(event: Noted) -> None: ...

    @handlers.after_commit(Noted)
    async def two(event: Noted) -> None: ...

    with pytest.raises(EventRegistrationError, match="more than 1 after_commit"):
        bus_of(handlers, max_handlers=1)


async def test_subject_selects_handlers_for_one_class() -> None:
    seen: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(About[Note])
    async def notes(event: About[Note]) -> None:
        seen.append("note")

    @handlers.after_commit(About)
    async def every(event: About[object]) -> None:
        seen.append("any")

    bus = bus_of(handlers)
    await bus.emit(About(Note))
    await bus.emit(About(Other))
    assert seen == ["note", "any", "any"]


async def test_in_transaction_failure_rolls_back_and_stops_later_handlers(
    database: Database,
) -> None:
    ran: list[str] = []
    handlers = events.Handlers()

    @handlers.in_transaction(Noted)
    async def write(event: Noted) -> None:
        await database.execute("INSERT INTO seen (name) VALUES (?)", event.text)
        ran.append("write")

    @handlers.in_transaction(Noted)
    async def refuse(event: Noted) -> None:
        raise ValueError("no")

    @handlers.in_transaction(Noted)
    async def never(event: Noted) -> None:
        ran.append("never")

    bus = bus_of(handlers)
    with events.use(bus), pytest.raises(ValueError, match="no"):
        async with database.atomic():
            await bus.emit(Noted("a"))
    assert ran == ["write"]
    assert await database.fetch("SELECT name FROM seen") == []
    assert bus.stats()[("Noted", "in_transaction", "error")] == 1


async def test_in_transaction_needs_an_open_transaction(database: Database) -> None:
    handlers = events.Handlers()

    @handlers.in_transaction(Noted)
    async def handle(event: Noted) -> None: ...

    with pytest.raises(EventError, match="no transaction is open"):
        await bus_of(handlers).emit(Noted("a"))


async def test_after_commit_waits_for_the_commit_and_dies_with_a_rollback(
    database: Database,
) -> None:
    visible: list[int] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    async def after(event: Noted) -> None:
        rows = await database.fetch("SELECT name FROM seen")
        visible.append(len(rows))

    bus = bus_of(handlers)
    with events.use(bus):
        async with database.atomic():
            await database.execute("INSERT INTO seen (name) VALUES ('a')")
            await bus.emit(Noted("a"))
            assert visible == []
        assert visible == [1]

        async def fail_after_emit() -> None:
            async with database.atomic():
                await bus.emit(Noted("b"))
                raise RuntimeError

        with pytest.raises(RuntimeError):
            await fail_after_emit()
        assert visible == [1]
        await bus.emit(Noted("c"))  # no transaction: delivered at once
        assert visible == [1, 1]


async def test_after_commit_failure_is_logged_counted_and_isolated(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    async def broken(event: Noted) -> None:
        raise ValueError("boom")

    @handlers.after_commit(Noted)
    async def fine(event: Noted) -> None:
        ran.append("fine")

    bus = bus_of(handlers)
    with caplog.at_level(logging.ERROR), events.use(bus):
        async with database.atomic():
            await bus.emit(Noted("a"))
    assert ran == ["fine"]
    assert "broken failed" in caplog.text
    stats = bus.stats()
    assert stats[("Noted", "after_commit", "error")] == 1
    assert stats[("Noted", "after_commit", "ok")] == 1


async def test_a_slow_handler_times_out(database: Database) -> None:
    handlers = events.Handlers()

    @handlers.in_transaction(Noted)
    async def slow(event: Noted) -> None:
        await asyncio.sleep(5)

    bus = bus_of(handlers, handler_timeout=0.05)
    with events.use(bus), pytest.raises(EventTimeout, match="timed out"):
        async with database.atomic():
            await bus.emit(Noted("a"))
    assert bus.stats()[("Noted", "in_transaction", "timeout")] == 1


async def test_nested_emits_stop_at_the_depth_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    async def again(event: Noted) -> None:
        calls.append(1)
        await events.emit(event)

    bus = bus_of(handlers, max_depth=3)
    with caplog.at_level(logging.ERROR), events.use(bus):
        await bus.emit(Noted("loop"))
    assert len(calls) == 3
    assert "nested deeper than 3" in caplog.text


async def test_capture_records_emitted_events_and_muted_skips_handlers() -> None:
    ran: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    async def handle(event: Noted) -> None:
        ran.append(event.text)

    bus = bus_of(handlers)
    assert not bus.listening(About)
    with bus.capture() as seen:
        assert bus.listening(About)
        await bus.emit(About(Note))
        await bus.emit(Noted("a"))
        with bus.muted():
            await bus.emit(Noted("b"))
    assert seen == [About(Note), Noted("a"), Noted("b")]
    assert ran == ["a"]


def test_emit_without_a_bus_says_so() -> None:
    with pytest.raises(EventError, match="no event bus is active"):
        events.current_events()


async def test_the_application_bus_serves_requests_and_sync_handlers() -> None:
    seen: list[str] = []
    handlers = events.Handlers("notes")

    @handlers.after_commit(Noted)
    def record(event: Noted) -> None:  # plain def: runs on the sync executor
        seen.append(event.text)

    @http.post("/noted")
    async def noted() -> dict[str, str]:
        await events.emit(Noted("hello"))
        return {"ok": "yes"}

    app = Hinoki(events=[handlers], include=[noted])
    assert [r.name.rsplit(".", 1)[1] for r in app.events.registrations] == ["record"]
    async with ASGIClient(app) as client:
        assert (await client.post("/noted")).status == 200
    assert seen == ["hello"]


async def test_a_sync_handler_needs_the_worker_executor() -> None:
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    def record(event: Noted) -> None: ...

    with pytest.raises(EventError, match="no worker executor"):
        await bus_of(handlers).call_sync(bus_of(handlers).registrations[0], Noted("x"))


def test_settings_validate_the_limits() -> None:
    for bad in ({"handler_timeout": 0}, {"max_handlers": 0}, {"max_depth": 0}):
        with pytest.raises(ValueError, match="must be"):
            EventsSettings(**bad).validate()


class EmitFromCommand(Command):
    name = "emit"

    async def handle(self, _context: CommandContext, _args: Namespace) -> None:
        await events.emit(Noted("cmd"))


async def test_a_command_can_emit_to_sync_handlers() -> None:
    seen: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    def record(event: Noted) -> None:
        seen.append(event.text)

    app = Hinoki(events=[handlers], commands=[EmitFromCommand])
    assert await run_command(app, app.commands[0], Namespace()) == 0
    assert seen == ["cmd"]


async def test_startup_and_shutdown_hooks_can_emit_to_sync_handlers() -> None:
    seen: list[str] = []
    handlers = events.Handlers()

    @handlers.after_commit(Noted)
    def record(event: Noted) -> None:
        seen.append(event.text)

    async def hook(_ctx: ModuleContext) -> None:
        await events.emit(Noted("hook"))

    app = Hinoki(events=[handlers], on_startup=[hook], on_shutdown=[hook])
    async with ASGIClient(app):
        assert seen == ["hook"]
    assert seen == ["hook", "hook"]
