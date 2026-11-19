---
status: implemented
---

# 6. Run and test it

## Settings and serving

The Tracker example's factory assembles the HTTP routes, rules and MCP tool; the `database` field of its settings opens the database. It uses typed `TrackerSettings`; local SQLite is the default and `TRACKER_DATABASE__URL` selects another database.

```python
from tracker import api
from tracker.rules import tracker_rules
from tracker.security import member_only
from tracker.settings import TrackerSettings
from hinoki import Hinoki
from hinoki.ai.mcpapp import expose_as_mcp


def create_app(database_url: str | None = None) -> Hinoki:
    overrides = (
        {"database": {"url": database_url}} if database_url is not None else {}
    )
    app_settings = TrackerSettings.load(overrides=overrides)
    app = Hinoki(
        settings=app_settings,
        title="Tracker",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        rules=tracker_rules,
    )
    app.include(api.health, api.tasks)
    app.include(
        *expose_as_mcp(app, tools=[f"{api.__name__}.list_tasks"], access=member_only)
    )
    return app
```

```sh
# Omit these variables to use .hinoki/local.sqlite3.
export TRACKER_DATABASE__URL='postgresql://user:password@127.0.0.1:5432/tracker'
export HINOKI_DATABASE_URL="$TRACKER_DATABASE__URL"
uv run --project .. hinoki migrations status
uv run --project .. hinoki migrations sql --app tracker 0001-initial
uv run --project .. hinoki migrations sql --app tracker 0002-indexes
uv run --project .. hinoki migrations apply --app tracker
uv run --project .. hinoki dev tracker.app:create_app
```

The Tracker example already includes its initial and index migrations, so this
command sequence inspects and applies those checked-in files. After changing a
model, generate a new migration with `hinoki migrations plan --app tracker`
and review its SQL before applying it to a disposable database; do not expect
the planner to recreate the existing initial migration.
`dev` reloads on change; `serve` runs without reload and takes host, port and
workers from the `server` settings ([serving](../serving.md)). Open `/docs` or
`/redoc`.

## Test it

Operations need no HTTP at all, so test them directly; then drive the whole
stack with the in-process client. The repository test fixture applies the
migrations and creates the Ann and Ben members used by the bearer tokens below:
[tests/trackertest.py](https://github.com/caspel26/hinoki/blob/main/tests/trackertest.py).

```python
import os

import pytest

from tracker.app import create_app
from tracker.models import Member
from hinoki.testing import ASGIClient

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(URL is None, reason="no database")


@needs_database
async def test_tracker_api(members: tuple[Member, Member]) -> None:
    assert URL is not None
    async with ASGIClient(create_app(URL), strict_errors=True) as client:
        made = await client.post("/tasks", json={"title": "x"}, token="ann-token")
        path = f"/tasks/{made.body['id']}"
        assert (await client.post(f"{path}/complete", token="ben-token")).status == 404
        assert (await client.post(f"{path}/complete", token="ann-token")).status == 200
        assert (await client.post(f"{path}/complete", token="ann-token")).status == 409
```

This is an excerpt of `test_every_business_error_of_the_tracker_is_declared`;
its `members` fixture creates the two users and provides a fresh migrated
database. Run the repository test with a disposable PostgreSQL URL:

```sh
HINOKI_TEST_DATABASE_URL="$TRACKER_DATABASE__URL" uv run pytest tests/trackertest.py
```

`strict_errors=True` fails the test if an action answers an error it did not
declare. For tests without `async`, use `SyncClient`.

## Where next

[Patterns and best practices](../patterns.md), [the application MCP](../app-mcp.md) (the tracker already exposes `list_tasks` to MCP clients), and [development tools](../ai-workflow.md).
