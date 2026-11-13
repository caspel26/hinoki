# Getting started

This guide installs Hinoki from its repository, creates a starter project and runs an API. Hinoki is pre-alpha and is not published on PyPI.

## Install

```sh
git clone https://github.com/caspel26/hinoki && cd hinoki
uv sync --locked            # builds the Rust extension and installs the dev tools
```

You need [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain.

## Create a project

```sh
uv run hinoki new bookmarks --dir ..
cd ../bookmarks && uv sync && uv run pytest
```

The generated project has a health route, typed settings, default rules, an `AGENTS.md` with project commands and conventions, and a `.mcp.json` for the development MCP server. `uv sync` builds the native extension and installs the generated app's dependencies.

## Run the small Notes example

`examples/notes/app.py` contains the model, two `Schema` classes and four actions. `examples/notes/main.py` declares the app with typed settings and a `DatabaseModule`; the factory in `app.py` remains useful for tests that pass a disposable database URL directly.

```python
@get("/notes/{note_id}")
async def one(note_id: UUID) -> NoteOut:
    return NoteOut.from_model(await Note.aget(id=note_id))


@post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))
```

Path, query and body values are typed parameters; the `Request` object is optional (declare `request: Request` first when you need headers, cookies or the raw body). `Note.acreate(data)` takes the validated schema directly, and extra keywords are supplied by application code.

## Create the database table

Create an empty PostgreSQL database, then set the URL used by both the migration command and the app:

```sh
createdb notes
export NOTES_DATABASE__URL=postgres://postgres:pw@127.0.0.1:5432/notes
```

The Notes example starts without a migration file. Generate one from its model, inspect the SQL, then apply it explicitly. The planner writes a numbered `.sql` migration and a matching `.state.json` file under `examples/notes/migrations`; the command never runs as part of app startup.

```sh
uv run hinoki migrations --dir examples/notes/migrations plan \
  --models examples.notes.app --name initial
uv run hinoki migrations --dir examples/notes/migrations sql 0001-initial
```

Read the generated migration before applying it. Use a disposable database while evaluating the example:

```sh
uv run hinoki migrations --dir examples/notes/migrations apply \
  --database "$NOTES_DATABASE__URL"
```

## Start and call the API

```sh
uv run hinoki dev examples.notes.main:app
```

The database must exist and be reachable before you apply the migration. The app's `DatabaseModule` opens the worker pool when the server starts and closes it at shutdown. It does not create tables. Open `/docs` (Swagger UI) or `/redoc`.

Create a note and list the collection:

```sh
curl -i -X POST http://127.0.0.1:8000/notes \
  -H 'content-type: application/json' \
  -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The first request returns `201 Created` with a JSON object containing the generated `id`, `title` and `body`; the collection request returns a JSON array. Invalid input is rejected before the handler runs. See [errors](ERRORS.md) for response behavior.

## What next

* Follow the [tracker tutorial](tutorial/index.md) for models, operations, endpoints, rules and tests.
* Read [models and queries](MODELS.md), [authentication](AUTHENTICATION.md) and [application modules](MODULES.md) for focused guides.
* See [settings](SETTINGS.md), [serving](SERVING.md) and [migrations](MIGRATIONS.md) before running against a shared database.
* Look up public symbols in the [API reference](reference/index.md).
