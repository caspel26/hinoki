# Tracker

Tracker demonstrates a layered app: typed HTTP routes in `api.py`, reusable
business operations in `operations.py`, persistence in `models.py`, and
optional architecture rules in `rules.py`. Its task endpoints scope every
read/write to the authenticated member. The app also exposes the task listing
as an authenticated MCP tool.

## Run locally

The checked-in initial and index migrations can be applied to the default local
SQLite database before starting the app. Run these commands from `examples/` so
`--project ..` selects the repository's locked Python environment:

```sh
cd examples
uv run --project .. hinoki app list
uv run --project .. hinoki migrations status
uv run --project .. hinoki migrations plan --app tracker --dry-run
uv run --project .. hinoki migrations sql --app tracker 0001-initial
uv run --project .. hinoki migrations sql --app tracker 0002-indexes
uv run --project .. hinoki migrations apply --app tracker
uv run --project .. hinoki dev tracker.app:create_app
```

The local database is `.hinoki/local.sqlite3`. For another backend, set
`TRACKER_DATABASE__URL`; set `HINOKI_DATABASE_URL` to that same URL for
migration commands. Open `/docs` for the generated OpenAPI document. Status
shows all registered apps; `--app tracker` scopes plans and applies to Tracker
and its declared dependencies.

Members need a `Member` row with an opaque token to use the API. The API
provides health, paginated task listing, creation, completion and deletion;
completed tasks cannot be deleted. The repository tests create synthetic
members and use a disposable PostgreSQL database.

The manifest registers Notes, Tracker and Blog as separate apps, each with its
own models and migrations. `hinoki migrations apply` initializes all three in
dependency order.
