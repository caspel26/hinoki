# Notes

The smallest complete Hinoki app: a model, input/output schemas, CRUD handlers,
and a declared database module. Its routes are all in `app.py`; `main.py`
assembles the ASGI app.

## Run locally

From the repository root, generate and apply the initial migration, then start
the app:

```sh
uv run hinoki migrations --dir examples/notes/migrations plan \
  --models examples.notes.app
uv run hinoki migrations --dir examples/notes/migrations apply
uv run hinoki dev examples.notes.main:app
```

SQLite is the default at `.hinoki/local.sqlite3`. Open `/docs` to inspect the
generated OpenAPI UI. Set `NOTES_DATABASE__URL` to select another backend; set
`HINOKI_DATABASE_URL` to the same URL when running the explicit migration
commands.

The example exposes `GET /notes`, `GET /notes/{note_id}`, `POST /notes` and
`DELETE /notes/{note_id}`. It is intentionally small and has no auth or admin.
