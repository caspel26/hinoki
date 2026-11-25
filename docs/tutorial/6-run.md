---
status: implemented
related:
  - settings.md
  - serving.md
  - testing.md
---

# 6. Run and test it

## Settings

Settings are a typed class. Tracker declares a database; with no value set it
is local SQLite. Each field of `DatabaseSettings` is a `TRACKER_DATABASE__<FIELD>`
variable, two underscores between the group and the field, so
`TRACKER_DATABASE__URL` selects another database ([Configure a
database](../databases.md)):

```python
# tracker/settings.py
from hinoki import settings


class TrackerSettings(settings.AppSettings, prefix="TRACKER_"):
    database: settings.DatabaseSettings
```

`main.py` declares the application at module level. It names no route, event
handler, middleware or rule, because `Hinoki()` reads them from `hinoki.toml`:

```python
# tracker/main.py
from hinoki import Hinoki

from .commands import Seed
from .settings import TrackerSettings


def create_app(settings: TrackerSettings | None = None) -> Hinoki:
    """Routes, events, middleware and rules come from the manifest."""
    return Hinoki(
        settings=settings or TrackerSettings,
        title="Tracker",
        openapi_url="/openapi.json",
        docs_url="/docs",
        commands=[Seed],
    )


app = create_app()
```

What the manifest ([the index](index.md#the-trackers-shape)) supplies:

| `hinoki.toml` | What `Hinoki()` loads |
| --- | --- |
| `[project] routes` | `tracker/routes.py`: `GET /health` |
| `[[apps]] routes` | each app's routes, at `/<app name>` |
| `[[apps]] events` | the handlers of `activity` |
| `[project] middleware` | `ApiVersion`, which adds `x-tracker-version` to every answer of a matched route |
| `[project] rules` | `policy` and `layers`, checked at startup and by `hinoki check` |

`Hinoki()` fills each of `include=`, `events=`, `middleware=` and `rules=` from
the manifest when you leave it out, and only in the module that
`[project] application` names (`tracker.main`). An argument you pass yourself,
even an empty one, replaces the manifest's list for that argument; do that only
when you want an explicit application, such as a test that serves one app.
`create_app` takes the settings so a test can point it at its own database.

`Hinoki(settings=TrackerSettings)` reads the `TRACKER_*` variables at import;
nothing connects until a worker starts.

## Serve it

From `examples/tracker`:

=== "SQLite (default)"

    ```sh
    uv run hinoki migrations apply
    uv run hinoki manage seed
    uv run hinoki dev
    ```

=== "PostgreSQL"

    ```sh
    export TRACKER_DATABASE__URL=postgresql://user:password@127.0.0.1:5432/tracker
    uv run hinoki migrations apply
    uv run hinoki manage seed
    uv run hinoki dev
    ```

The migration commands, `manage` and the server all read the `database` setting
of the application that `hinoki.toml` names, so `TRACKER_DATABASE__URL` alone
configures all of them ([which database](../migrations.md#which-database)).
`hinoki dev` serves on `http://127.0.0.1:8000` and reloads on change; open
`/docs`. `hinoki serve` runs without reload and takes host, port and workers
from the `server` settings ([serving](../serving.md)).

## Try it

Every task route needs a member's token, and the Tracker has no sign-up route.
`hinoki manage seed` is a management command of the example
(`tracker/commands.py`) that creates the member `Ada Demo` and prints a random token once; the table stores only its SHA-256 digest, so the token cannot be shown again. Export it:

```text
member 'Ada Demo' created; token '<random token>' (shown once)
```

```sh
export TOKEN='<the printed token>'
```

With the server running, call the API from a second terminal:

```sh
curl -X POST http://127.0.0.1:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"title":"Write docs","priority":2}'
curl 'http://127.0.0.1:8000/tasks?done=false&priority=1,2&search=docs&ordering=-created_at' \
  -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/activity -H "Authorization: Bearer $TOKEN"
```

The first call answers `201` with the new task, the second a page with it that
matches the filters, and the third the `created` line that the `activity` app
wrote. Without the header every one of them answers `401`.

```text
{"id":"…","title":"Write docs","priority":2,"done":false,"created_at":"…","completed_at":null}
```

Completing the task answers `200`; completing it again is a `409`
`task_already_done`, and deleting a completed task a `409`
`done_tasks_are_kept`:

```sh
curl -X POST http://127.0.0.1:8000/tasks/<id>/complete -H "Authorization: Bearer $TOKEN"
```

## Test it

The tests of the Tracker need no environment variable: the fixture in
`tracker_tests/conftest.py` migrates each app, in the order of its `requires`,
into a SQLite file in a temporary directory, creates the members Ann and Ben and
builds the application with `create_app(settings)` around that file:

```python
# tracker_tests/conftest.py (excerpt)
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
```

Operations need no HTTP, so test them directly. Then drive the whole stack with
the in-process client:

```python
# tracker_tests/trackerapitest.py (excerpt)
async def test_completing_twice_is_a_conflict_and_done_tasks_are_kept(
    world: World,
) -> None:
    task = await create(world, "Ship")
    path = f"/tasks/{task['id']}"
    ann = "ann-token"

    done = await world.client.post(f"{path}/complete", token=ann)
    again = await world.client.post(f"{path}/complete", token=ann)
    removed = await world.client.delete(path, token=ann)

    assert (done.status, done.body["done"]) == (200, True)
    assert (again.status, again.body["error"]) == (409, "task_already_done")
    assert (removed.status, removed.body["error"]) == (409, "done_tasks_are_kept")
```

Run all of them from the repository root:

```sh
uv run --locked pytest examples/tracker/tracker_tests
```

`ASGIClient(app, strict_errors=True)` fails a test when an action answers an
error it did not declare. For tests without `async`, use `SyncClient`. See
[testing](../testing.md) for disposable databases of other backends.

## Recap

- Settings are typed and read from prefixed environment variables.
- `hinoki.toml` wires routes, events, middleware and rules; `main.py` only
  configures the application.
- `hinoki dev` serves with reload; migrations stay a separate step.
- Test operations directly and the stack with `ASGIClient`.

Next: [7. Validate input](7-validation.md).
