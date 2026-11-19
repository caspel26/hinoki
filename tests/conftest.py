"""Shared fixtures."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.harness import Pool


@pytest.fixture
async def pool() -> AsyncIterator[Pool]:
    clients = Pool()
    yield clients
    for client in clients.clients:
        if client.task is not None and not client.task.done():
            await client.lifespan_event("shutdown")


DATABASE_VARIABLES = (
    "HINOKI_TEST_DATABASE_URL",
    "HINOKI_TEST_MYSQL_URL",
    "HINOKI_TEST_MARIADB_URL",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests that share the CI database run on one pytest-xdist worker
    (``--dist loadgroup``), one after the other."""
    reads: dict[Path, bool] = {}
    for item in items:
        path = item.path
        if path not in reads:
            text = path.read_text(encoding="utf-8")
            reads[path] = any(name in text for name in DATABASE_VARIABLES)
        if reads[path]:
            item.add_marker(pytest.mark.xdist_group("database"))
