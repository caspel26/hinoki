# Notes

The easy example: an API in five minutes. One app, one model and five generated
actions. There is no sign-in, no rules and no admin, on purpose: Tracker adds
the access policies and Blog the rest.

## Read it in this order

| # | File | What to see |
| --- | --- | --- |
| 1 | [`hinoki.toml`](hinoki.toml) | The project registry: the application and the one app, with its models and migrations (lines 1-8) |
| 2 | [`notes/apps/notes/models.py`](notes/apps/notes/models.py) | The `Note` table: `@db.table` pins the name, `@db.ordered_by` sets the default order (lines 9-14) |
| 3 | [`notes/apps/notes/schemas.py`](notes/apps/notes/schemas.py) | The request body `NoteIn` (line 11), the partial `NotePatch` derived from the model (line 16) and the response `NoteOut` (line 21): every request and response is a `Schema` |
| 4 | [`notes/apps/notes/routes.py`](notes/apps/notes/routes.py) | One `crud(...)` value generates list, retrieve, create, update and delete (lines 8-18) |
| 5 | [`notes/settings.py`](notes/settings.py) | Typed settings: the `NOTES_` variables and a database (lines 6-7) |
| 6 | [`notes/main.py`](notes/main.py) | `create_app` builds the application (line 12); `app` is created once at import and opens nothing (line 23) |
| 7 | [`notes_tests/notesapitest.py`](notes_tests/notesapitest.py) | Four tests through the whole stack, with the fixture in [`notes_tests/conftest.py`](notes_tests/conftest.py): a migrated SQLite file per test |

`notes/apps/notes/migrations/` holds the checked-in SQL for the table; review it
before applying.

## Run

From this directory (`examples/notes`); `uv` finds the repository's environment
above it:

```sh
uv run hinoki migrations apply
uv run hinoki dev
```

`migrations apply` creates `.hinoki/local.sqlite3` here. The application never
migrates at startup. In another terminal:

```sh
curl -X POST http://127.0.0.1:8000/notes -H 'content-type: application/json' -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The `POST` answers `201 Created` with the new note; the `GET` answers a page:
`{"items": [...], "page": 1, "page_size": 20, "total": 1, "pages": 1}`. Invalid
input is refused with `422` before the handler runs. Open `/docs` for the
OpenAPI UI.

| Route | Action |
| --- | --- |
| `GET /notes` | list notes, ordered by title, paginated |
| `GET /notes/{item_id}` | one note, or 404 |
| `POST /notes` | create a note |
| `PATCH /notes/{item_id}` | change the title or the body |
| `DELETE /notes/{item_id}` | delete a note |

`uv run hinoki routes` lists them without starting a server. Set
`NOTES_DATABASE__URL` to use another database; the migration commands read it
too.

## Test

From the repository root:

```sh
uv run --locked pytest examples/notes/notes_tests
```

The tests need no environment variable: each one migrates its own SQLite file in
a temporary directory, then talks to the application with `ASGIClient`.
