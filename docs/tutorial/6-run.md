---
status: implemented
related:
  - settings.md
  - serving.md
  - testing.md
---

# 6. Run and test it

## Settings

Settings are a typed class. Tracker declares a database; local SQLite is the
default, and `TRACKER_DATABASE__URL` selects another database:

```python
# tracker/settings.py
from hinoki import settings


class TrackerSettings(settings.AppSettings, prefix="TRACKER_"):
    database: settings.DatabaseSettings
```

`main.py` declares the application at module level, with its rules, routes and
an MCP tool:

```python
# tracker/main.py
from hinoki import Hinoki
from hinoki.ai.mcpapp import expose_as_mcp

from . import api
from .rules import tracker_rules
from .security import member_only
from .settings import TrackerSettings

app = Hinoki(
    settings=TrackerSettings,
    title="Tracker",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    include=[api.health, api.tasks],
    rules=tracker_rules,
)
# The MCP endpoint exposes only this action and uses the caller's own token.
app.include(
    *expose_as_mcp(app, tools=[f"{api.__name__}.list_tasks"], access=member_only)
)
```

`Hinoki(settings=TrackerSettings)` reads the `TRACKER_*` variables at import;
nothing connects until a worker starts. A project created with `hinoki new`
writes the same module, with `include=manifest_routes()` in place of the list
([chapter 8](8-projects.md)).

## Serve it

From `examples/`:

=== "SQLite (default)"

    ```sh
    uv run hinoki migrations apply tracker
    uv run hinoki dev tracker.main:app
    ```

=== "PostgreSQL"

    ```sh
    export TRACKER_DATABASE__URL=postgresql://user:password@127.0.0.1:5432/tracker
    export EXAMPLES_DATABASE__URL=$TRACKER_DATABASE__URL
    uv run hinoki migrations apply tracker
    uv run hinoki dev tracker.main:app
    ```

The migration commands use the `database` setting of the project application
named in `examples/hinoki.toml` (`project.app:app`, configured by
`EXAMPLES_DATABASE__URL`), not the tracker's own settings. In a project whose
application is the tracker, `TRACKER_DATABASE__URL` alone configures both
([which database](../migrations.md#which-database)). `hinoki dev` serves on
`http://127.0.0.1:8000` and reloads on change; open `/docs` or `/redoc`.
`hinoki serve` runs without reload and takes host, port and workers from the
`server` settings ([serving](../serving.md)).

## Test it

The application reads its settings at import, so the test configuration sets
the database variable first. In the repository, `tests/conftest.py` copies
`HINOKI_TEST_DATABASE_URL` to `TRACKER_DATABASE__URL` before any test module
imports `tracker.main` ([testing](../testing.md#use-a-test-database)).

Operations need no HTTP, so test them directly. Then drive the whole stack with
the in-process client:

```python
# tests/trackertest.py (excerpt)
from tracker.main import app


@needs_database
async def test_every_business_error_of_the_tracker_is_declared(
    members: tuple[Member, Member],
) -> None:
    async with ASGIClient(app, strict_errors=True) as client:
        made = await client.post("/tasks", json={"title": "x"}, token="ann-token")
        path = f"/tasks/{made.body['id']}"
        assert (await client.post(f"{path}/complete", token="ben-token")).status == 404
        assert (await client.post(f"{path}/complete", token="ann-token")).status == 200
        again = await client.post(f"{path}/complete", token="ann-token")
        assert again.status == 409
```

The `members` fixture of
[`tests/trackertest.py`](https://github.com/caspel26/hinoki/blob/main/tests/trackertest.py)
migrates a fresh database and creates the members Ann and Ben. Run it from the
repository root against a disposable database, which the fixture empties first:

```sh
export HINOKI_TEST_DATABASE_URL=sqlite://./.hinoki/tracker-test.sqlite3
uv run pytest tests/trackertest.py
```

`strict_errors=True` fails a test when an action answers an error it did not
declare. For tests without `async`, use `SyncClient`.

## Recap

- Settings are typed and read from prefixed environment variables.
- `hinoki dev` serves with reload; migrations stay a separate step.
- Test operations directly and the stack with `ASGIClient`.

Next: [7. Validate input](7-validation.md).
