"""Application ``on_startup`` / ``on_shutdown`` hooks around the module lifespans."""

import asyncio
import threading
from argparse import Namespace
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki import Hinoki, Module, ModuleContext, RuntimeConfig, http
from hinoki.db import Database, DatabaseModule
from hinoki.errors import RegistrationError, ShutdownError, StartupError
from hinoki.management import Command, CommandContext
from hinoki.orm import Model, field
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.lifespan import Hook, Lifespan
from hinoki.runtime.management import run_command
from hinoki.settings import AppSettings
from hinoki.testing import ASGIClient
from hinoki.wsgi import WSGIAdapter
from tests.wsgitest import call


class Label:
    def __init__(self, text: str) -> None:
        self.text = text


class Store(Module):
    name = "store"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        self.events.append("module:start")
        ctx.provide(Label, Label("ready"))
        try:
            yield
        finally:
            self.events.append("module:stop")


def note(events: list[str], text: str) -> Hook:
    async def hook(ctx: ModuleContext) -> None:
        events.append(f"{text}:{ctx.get(Label).text}")

    return hook


def failing(events: list[str], text: str) -> Hook:
    async def broken(ctx: ModuleContext) -> None:
        events.append(text)
        raise RuntimeError("secret-value-123")

    return broken


def hanging(events: list[str], text: str) -> Hook:
    async def stuck(ctx: ModuleContext) -> None:
        events.append(text)
        await asyncio.sleep(60)

    return stuck


def build(
    events: list[str],
    on_startup: list[Hook],
    on_shutdown: list[Hook],
    **config: float,
) -> Lifespan:
    return Lifespan(
        [Store(events)],
        ModuleContext(),
        SyncExecutor(2, 2),
        RuntimeConfig(**config),  # type: ignore[arg-type]
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


async def test_hooks_run_inside_the_module_lifespans_in_listed_order() -> None:
    events: list[str] = []
    app = Hinoki(
        include=[ping],
        modules=[Store(events)],
        on_startup=[note(events, "up1"), note(events, "up2")],
        on_shutdown=[note(events, "down1"), note(events, "down2")],
    )
    async with ASGIClient(app) as client:
        assert events == ["module:start", "up1:ready", "up2:ready"]
        assert (await client.get("/ping")).status == 200
    assert events[3:] == ["down1:ready", "down2:ready", "module:stop"]


async def test_plain_def_hooks_run_on_the_sync_executor() -> None:
    threads: list[str] = []
    seen: list[object] = []

    def sync_hook(ctx: ModuleContext) -> None:
        threads.append(threading.current_thread().name)
        seen.append(ctx.settings)

    settings = AppSettings()
    app = Hinoki(
        settings=settings,
        include=[ping],
        on_startup=[sync_hook],
        on_shutdown=[sync_hook],
    )
    async with ASGIClient(app):
        pass
    assert len(threads) == 2
    assert threading.current_thread().name not in threads
    assert seen == [settings, settings]


async def test_a_failing_startup_hook_stops_startup_and_skips_shutdown_hooks() -> None:
    events: list[str] = []
    life = build(
        events,
        [note(events, "up1"), failing(events, "up2"), note(events, "up3")],
        [note(events, "down")],
    )
    with pytest.raises(StartupError) as caught:
        await life.startup()
    assert not life.ready
    assert events == ["module:start", "up1:ready", "up2", "module:stop"]
    assert caught.value.module == "on_startup:broken"
    assert str(caught.value) == "startup hook 'broken' failed to start: RuntimeError"
    await life.shutdown()  # nothing left to do, and no shutdown hook runs
    assert events[-1] == "module:stop"


async def test_a_failed_startup_reaches_the_server_and_requests_stay_503() -> None:
    events: list[str] = []
    app = Hinoki(include=[ping], on_startup=[failing(events, "up")])
    with pytest.raises(RuntimeError, match="startup hook 'broken' failed"):
        async with ASGIClient(app):
            pass
    adapter = WSGIAdapter(app)
    try:
        assert call(adapter, "GET", "/ping")[0] == "503 Service Unavailable"
    finally:
        adapter.close()


async def test_a_hung_startup_hook_is_cut_by_the_startup_timeout() -> None:
    events: list[str] = []
    life = build(events, [hanging(events, "up")], [], startup_timeout=0.1)
    with pytest.raises(StartupError) as caught:
        await life.startup()
    assert isinstance(caught.value.cause, TimeoutError)
    assert events == ["module:start", "up", "module:stop"]


async def test_every_shutdown_hook_and_the_modules_run_after_a_failure() -> None:
    events: list[str] = []
    life = build(
        events,
        [],
        [failing(events, "down1"), hanging(events, "down2"), note(events, "down3")],
        shutdown_timeout=0.1,
    )
    await life.startup()
    with pytest.raises(ShutdownError) as caught:
        await life.shutdown()
    assert events == ["module:start", "down1", "down2", "down3:ready", "module:stop"]
    assert [type(cause) for cause in caught.value.causes] == [
        RuntimeError,
        TimeoutError,
    ]


async def test_the_worker_is_not_ready_while_shutdown_hooks_run() -> None:
    states: list[bool] = []
    holder: list[Lifespan] = []

    async def look(ctx: ModuleContext) -> None:
        states.append(holder[0].ready)

    holder.append(build([], [look], [look]))
    await holder[0].startup()
    assert holder[0].ready
    await holder[0].shutdown()
    assert states == [False, False]  # ready only between the two


class Visit(Model, table="hook_visits"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    place: str = ""


async def test_models_reach_the_database_inside_a_hook(tmp_path: Path) -> None:
    counts: list[int] = []

    async def seed(ctx: ModuleContext) -> None:
        await ctx.get(Database).execute(
            "CREATE TABLE hook_visits (id text PRIMARY KEY, place text NOT NULL)"
        )
        await Visit(place="home").asave()

    def count(ctx: ModuleContext) -> None:
        counts.append(len(Visit.all().list()))  # sync model call in a def hook

    app = Hinoki(
        include=[ping],
        modules=[DatabaseModule(f"sqlite://{tmp_path / 'hooks.sqlite3'}")],
        on_startup=[seed],
        on_shutdown=[count],
    )
    async with ASGIClient(app):
        pass
    assert counts == [1]


@pytest.mark.parametrize("hooks", [[None], ["name"], "name", ping])
def test_only_lists_of_functions_are_accepted(hooks: object) -> None:
    with pytest.raises(RegistrationError, match="on_startup"):
        Hinoki(on_startup=hooks)  # type: ignore[arg-type]


class Report(Command):
    name = "report"

    def handle(self, _context: CommandContext, _args: Namespace) -> int:
        return 0


def test_management_commands_do_not_run_the_hooks() -> None:
    events: list[str] = []
    record: Callable[[ModuleContext], None] = lambda _ctx: events.append("hook")  # noqa: E731
    app = Hinoki(commands=[Report], on_startup=[record], on_shutdown=[record])
    assert asyncio.run(run_command(app, app.commands[0], Namespace())) == 0
    assert events == []


def test_the_wsgi_adapter_runs_the_hooks_once() -> None:
    events: list[str] = []
    app = Hinoki(
        include=[ping],
        modules=[Store(events)],
        on_startup=[note(events, "up")],
        on_shutdown=[note(events, "down")],
    )
    adapter = WSGIAdapter(app)
    assert events == []  # lazy, like the modules
    call(adapter, "GET", "/ping")
    call(adapter, "GET", "/ping")
    adapter.close()
    assert events == ["module:start", "up:ready", "down:ready", "module:stop"]
