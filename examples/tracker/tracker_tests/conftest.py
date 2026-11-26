"""A disposable SQLite database per test, migrated app by app in dependency order."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tracker.apps.members.models import Member
from tracker.apps.members.security import hash_token
from tracker.main import create_app
from tracker.settings import TrackerSettings
from trackerworld import World

from hinoki.db import Database
from hinoki.orm import migrator, use_database
from hinoki.testing import ASGIClient

APPS = Path(__file__).parents[1] / "tracker" / "apps"
ORDER = ("members", "tasks", "activity")  # the `requires` order of hinoki.toml


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    url = f"sqlite://{tmp_path / 'tracker.sqlite3'}"
    database = await Database.connect(url, max_size=1)
    for name in ORDER:
        await migrator.apply(database, APPS / name / "migrations", namespace=name)
    with use_database(database):
        ann = await Member.acreate(name="Ann", token_hash=hash_token("ann-token"))
        ben = await Member.acreate(name="Ben", token_hash=hash_token("ben-token"))
    database.close()
    settings = TrackerSettings.load(overrides={"database": {"url": url}})
    async with ASGIClient(create_app(settings)) as client:
        yield World(client, ann, ben)
