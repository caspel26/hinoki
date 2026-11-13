"""Shared fixtures."""

from collections.abc import AsyncIterator

import pytest

from tests.harness import Pool


@pytest.fixture
async def pool() -> AsyncIterator[Pool]:
    clients = Pool()
    yield clients
    for client in clients.clients:
        if client.task is not None and not client.task.done():
            await client.lifespan_event("shutdown")
