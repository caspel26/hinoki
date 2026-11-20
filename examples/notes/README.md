# Notes

The smallest complete Hinoki app, laid out the way `hinoki new notes` followed by
`hinoki app create notes` lays out a project: the `notes/` package holds only
`main.py` and the settings, and the `apps/notes/` app owns a model, input and
output schemas and one route group. `apps/notes/api.py` holds the schemas and the
`notes` group; `main.py` declares the application.

## Run

From `examples/`:

```sh
uv run hinoki migrations apply notes
uv run hinoki dev notes.main:app
```

The migration is checked in at
[`apps/notes/migrations/0001-create-table-notes.sql`](apps/notes/migrations/0001-create-table-notes.sql);
review it before applying. The database is `.hinoki/local.sqlite3`, and the
application never migrates at startup.

## Try it

In another terminal:

```sh
curl -X POST http://127.0.0.1:8000/notes -H 'content-type: application/json' -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The `POST` answers `201 Created` with the new note's `id`, `title` and `body`;
the `GET` answers a JSON array. Invalid input is rejected before the handler
runs. Open `/docs` for the OpenAPI UI.

| Route | Action |
| --- | --- |
| `GET /notes` | list notes, ordered by title |
| `GET /notes/{note_id}` | one note, or 404 |
| `POST /notes` | create a note |
| `DELETE /notes/{note_id}` | delete a note |

## Configure

Set `NOTES_DATABASE__URL` to serve another database, and `EXAMPLES_DATABASE__URL`
to the same URL for the migration commands (they use the examples' manifest
application).

To serve Notes as a WSGI callable instead (for a deployment that only runs
WSGI), use [`wsgi.py`](wsgi.py):

```sh
uv run hinoki serve notes.wsgi:wsgi_app --interface wsgi
```

Notes has no authentication, no admin and no rules on purpose: its routes are
public and its list is not paginated. Tracker and Blog enable every built-in rule.
