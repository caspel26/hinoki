"""A disposable SQLite database per test, migrated with the app's own migrations."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from notes.main import create_app
from notes.settings import NotesSettings

from hinoki.db import Database
from hinoki.orm import migrator
from hinoki.testing import ASGIClient

MIGRATIONS = Path(__file__).parents[1] / "notes" / "apps" / "notes" / "migrations"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[ASGIClient]:
    url = f"sqlite://{tmp_path / 'notes.sqlite3'}"
    database = await Database.connect(url, max_size=1)
    await migrator.apply(database, MIGRATIONS)
    database.close()
    settings = NotesSettings.load(overrides={"database": {"url": url}})
    async with ASGIClient(create_app(settings)) as client:
        yield client
