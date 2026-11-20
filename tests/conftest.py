"""Shared fixtures."""

import os
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


DATABASE_VARIABLES = (
    "HINOKI_TEST_DATABASE_URL",
    "HINOKI_TEST_MYSQL_URL",
    "HINOKI_TEST_MARIADB_URL",
)
# The examples' module-level apps read their settings at import, so their
# variables are set here, before any test module imports them.
EXAMPLE_DATABASES = ("NOTES", "TRACKER", "BLOG")
EXAMPLE_ADMIN_SECRET = "s" * 40  # synthetic: Blog's admin needs one to be included


def pytest_configure() -> None:
    """Under pytest-xdist each worker uses its own database (``hinoki_gw0``,
    ``hinoki_gw1``, ...), created beforehand, so workers never share tables."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is not None:
        for name in DATABASE_VARIABLES:
            url = os.environ.get(name)
            if url is not None:
                base, mark, query = url.partition("?")
                base = base.removesuffix("/hinoki") + f"/hinoki_{worker}"
                os.environ[name] = base + mark + query
    configure_examples()


def configure_examples() -> None:
    url = os.environ.get("HINOKI_TEST_DATABASE_URL")
    if url is not None:
        for prefix in EXAMPLE_DATABASES:
            os.environ[f"{prefix}_DATABASE__URL"] = url
    os.environ.setdefault("BLOG_ADMIN__SECRET", EXAMPLE_ADMIN_SECRET)
