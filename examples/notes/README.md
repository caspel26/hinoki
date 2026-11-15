# Notes

The smallest complete Hinoki app: a model, input/output schemas, CRUD handlers,
and a declared database module. Its routes are all in `app.py`; `main.py`
assembles the ASGI app.

## Run locally

From `examples/`, inspect the registered apps, plan the Notes migration, then
apply it and start the app. The locked Python/Rust environment is at the
repository root, so `--project ..` selects it:

```sh
uv run --project .. hinoki app list
uv run --project .. hinoki migrations plan --app notes
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes
uv run --project .. hinoki migrations apply --app notes
uv run --project .. hinoki dev notes.main:app
```

SQLite is the default at `.hinoki/local.sqlite3`. Open `/docs` to inspect the
generated OpenAPI UI. Set `NOTES_DATABASE__URL` to select another backend; set
`HINOKI_DATABASE_URL` to the same URL for migration commands. Migration
numbering and names are derived from the planned schema change; `--name` can
override the slug.

The example exposes `GET /notes`, `GET /notes/{note_id}`, `POST /notes` and
`DELETE /notes/{note_id}`. It is intentionally small and has no auth or admin.
