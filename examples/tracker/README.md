# Tracker

A layered app: HTTP routes in `api.py`, business operations in `operations.py`,
tables in `models.py` and architecture rules in `rules.py`. Every task read and
write is scoped to the authenticated member, and the task list is also an
authenticated MCP tool. The [tutorial](https://hinoki.caspel.dev/dev/tutorial/)
walks through it chapter by chapter.

## Run

From `examples/`:

```sh
uv run hinoki migrations apply tracker
uv run hinoki dev tracker.main:app
```

The checked-in migrations are in [`migrations/`](migrations/). The database is
`.hinoki/local.sqlite3`; set `TRACKER_DATABASE__URL` for another one, and
`EXAMPLES_DATABASE__URL` to the same URL for the migration commands. Open `/docs`
for the OpenAPI UI.

The API offers health, a paginated task list, creation, completion and deletion;
completed tasks cannot be deleted. Callers need a `Member` row with an opaque
token, sent as a bearer token. Create one in `uv run hinoki shell`:

```pycon
>>> await Member.acreate(name="Ann", token="ann-dev-token")
```

Then send `Authorization: Bearer ann-dev-token`, as in
[6. Run and test it](https://hinoki.caspel.dev/dev/tutorial/6-run/#try-it).

## Check

```sh
uv run hinoki check tracker.main:app
```

All built-in rules are on, plus three import-direction rules (13 in total); see
[rules](../../docs/rules.md).

## Test

From the repository root, with a disposable database (PostgreSQL or a
throwaway SQLite file), which the tests empty first:

```sh
export HINOKI_TEST_DATABASE_URL=sqlite://./.hinoki/tracker-test.sqlite3
uv run pytest tests/trackertest.py
```

The tests create synthetic members; never point them at real data.
