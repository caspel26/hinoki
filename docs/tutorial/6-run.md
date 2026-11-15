# 6. Run and test it

## Settings and serving

The Tracker example's factory assembles the HTTP routes, rules, MCP tool and database module. It reads the database URL from `TRACKER_DATABASE_URL`; the [settings guide](../settings.md) shows how to use a typed settings class instead.

```python
import os

from examples.tracker import api
from examples.tracker.rules import tracker_rules
from examples.tracker.security import member_only
from hinoki import Hinoki
from hinoki.db import DatabaseModule
from hinoki.mcpapp import expose_as_mcp


def create_app(database_url: str | None = None) -> Hinoki:
    app = Hinoki(
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
    app.install(DatabaseModule(database_url or os.environ["TRACKER_DATABASE_URL"]))
    return app
```

```sh
export TRACKER_DATABASE_URL='postgresql://user:password@127.0.0.1:5432/tracker'
export HINOKI_DATABASE_URL="$TRACKER_DATABASE_URL"
uv run hinoki migrations --dir examples/tracker/migrations plan \
    --models examples.tracker.models --name initial
uv run hinoki migrations --dir examples/tracker/migrations apply
uv run hinoki dev examples.tracker.app:create_app
```

Review the generated migration before applying it to a disposable database.
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

from examples.tracker.app import create_app
from examples.tracker.models import Member
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
HINOKI_TEST_DATABASE_URL="$TRACKER_DATABASE_URL" uv run pytest tests/trackertest.py
```

`strict_errors=True` fails the test if an action answers an error it did not
declare. For tests without `async`, use `SyncClient`.

## Where next

[Patterns and best practices](../patterns.md), [the application MCP](../app-mcp.md) (the tracker already exposes `list_tasks` to MCP clients), and [development tools](../ai-workflow.md).
