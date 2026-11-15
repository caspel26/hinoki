# Tracker

Tracker demonstrates a layered app: typed HTTP routes in `api.py`, reusable
business operations in `operations.py`, persistence in `models.py`, and
optional architecture rules in `rules.py`. Its task endpoints scope every
read/write to the authenticated member. The app also exposes the task listing
as an authenticated MCP tool.

## Run locally

The checked-in initial and index migrations can be applied to the default local
SQLite database before starting the app:

```sh
uv run hinoki migrations --dir examples/tracker/migrations status
uv run hinoki migrations --dir examples/tracker/migrations apply
uv run hinoki dev examples.tracker.app:create_app
```

The local database is `.hinoki/local.sqlite3`. For another backend, set
`TRACKER_DATABASE__URL`; set `HINOKI_DATABASE_URL` to that same URL for the
explicit migration command. Open `/docs` for the generated OpenAPI document.

Members need a `Member` row with an opaque token to use the API. The API
provides health, paginated task listing, creation, completion and deletion;
completed tasks cannot be deleted. The repository tests create synthetic
members and use a disposable PostgreSQL database.
