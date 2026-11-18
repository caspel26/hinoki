"""A ``database: DatabaseSettings`` field activates the database module."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

import pytest

from hinoki import Hinoki, Module, ModuleContext, Schema, http, settings
from hinoki.db import Database, DatabaseModule
from hinoki.errors import ModuleGraphError
from hinoki.orm import current_database
from hinoki.testing import ASGIClient


class AppSettings(settings.AppSettings, prefix="APP_"):
    database: settings.DatabaseSettings


class PlainSettings(settings.AppSettings, prefix="PLAIN_"):
    pass


class LoggedDatabase(DatabaseModule):
    pass


class NamedDatabase(Module):
    name = "database"


class NeedsDatabase(Module):
    name = "needs-database"
    requires: ClassVar[tuple[type[Module], ...]] = (DatabaseModule,)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        ctx.get(Database)
        yield


class MissingModule(Module):
    name = "missing"


class BrokenModule(Module):
    name = "broken"
    requires: ClassVar[tuple[type[Module], ...]] = (MissingModule,)


class Count(Schema):
    count: int


@http.post("/visits", status=201, atomic=True)
async def visit() -> Count:
    database = current_database()
    await database.execute("INSERT INTO visits DEFAULT VALUES")
    rows = await database.fetch("SELECT COUNT(*) AS count FROM visits")
    return Count(count=rows[0]["count"])


def database_modules(app: Hinoki) -> list[DatabaseModule]:
    return [module for module in app.modules if isinstance(module, DatabaseModule)]


def settings_with(url: str) -> AppSettings:
    return AppSettings.load(env={"APP_DATABASE__URL": url})


def test_a_database_settings_field_installs_the_module() -> None:
    app = Hinoki(settings=settings_with("sqlite://./custom.sqlite3"))
    (module,) = database_modules(app)
    assert module.url == "sqlite://./custom.sqlite3"
    assert database_modules(Hinoki(settings=PlainSettings.load(env={}))) == []
    assert database_modules(Hinoki()) == []


def test_a_settings_class_is_loaded_and_activates_the_database() -> None:
    (module,) = database_modules(Hinoki(settings=AppSettings))
    assert module.url.startswith("sqlite://")


def test_declared_database_modules_are_not_duplicated() -> None:
    loaded = settings_with("sqlite://./a.sqlite3")
    assert len(database_modules(Hinoki(settings=loaded, modules=[DatabaseModule]))) == 1
    explicit = DatabaseModule("sqlite://./explicit.sqlite3")
    assert database_modules(Hinoki(settings=loaded, modules=[explicit])) == [explicit]
    subclass = Hinoki(settings=loaded, modules=[LoggedDatabase])
    (module,) = database_modules(subclass)
    assert type(module) is LoggedDatabase


def test_custom_database_named_module_suppresses_automatic_database() -> None:
    custom = NamedDatabase()
    app = Hinoki(settings=settings_with("sqlite://./a.sqlite3"), modules=[custom])
    assert app.modules == (custom,)
    assert database_modules(app) == []


async def test_database_subclass_satisfies_a_base_module_dependency(
    tmp_path: Path,
) -> None:
    database = LoggedDatabase(f"sqlite://{tmp_path / 'explicit.sqlite3'}")
    dependent = NeedsDatabase()
    app = Hinoki(
        settings=settings_with("sqlite://./settings.sqlite3"),
        modules=[database, dependent],
    )
    assert app.modules == (database, dependent)
    async with ASGIClient(app):
        pass


def test_the_automatic_module_satisfies_dependent_modules() -> None:
    app = Hinoki(
        settings=settings_with("sqlite://./a.sqlite3"), modules=[NeedsDatabase()]
    )
    assert isinstance(app.modules[0], DatabaseModule)
    assert isinstance(app.modules[1], NeedsDatabase)


def test_a_later_install_replaces_the_automatic_module() -> None:
    app = Hinoki(settings=settings_with("sqlite://./a.sqlite3"))
    replacement = DatabaseModule("sqlite://./b.sqlite3")
    app.install(replacement)
    assert database_modules(app) == [replacement]


def test_failed_replacement_keeps_the_automatic_module_replaceable() -> None:
    app = Hinoki(settings=settings_with("sqlite://./a.sqlite3"))
    replacement = DatabaseModule("sqlite://./b.sqlite3")
    with pytest.raises(ModuleGraphError, match="requires MissingModule"):
        app.install(replacement, BrokenModule())
    assert app._automatic_database is database_modules(app)[0]
    app.install(replacement)
    assert database_modules(app) == [replacement]


async def test_the_automatic_pool_serves_transactional_actions(tmp_path: Path) -> None:
    url = f"sqlite://{tmp_path / 'visits.sqlite3'}"
    setup = await Database.connect(url)
    await setup.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY)")
    setup.close()
    app = Hinoki(
        settings=settings_with(url), include=[visit], modules=[NeedsDatabase()]
    )
    async with ASGIClient(app) as client:
        first = await client.post("/visits")
        second = await client.post("/visits")
    assert (first.status, first.body) == (201, {"count": 1})
    assert second.body == {"count": 2}
