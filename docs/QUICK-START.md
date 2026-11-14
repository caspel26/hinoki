# Quick start

This walkthrough runs the Notes example against a PostgreSQL database. It creates the table with an explicit migration; Hinoki does not create or migrate tables when the app starts.

## Create the database

Create an empty database and set the connection URL used by the migration command and the app:

```sh
createdb notes
export NOTES_DATABASE__URL=postgres://postgres:pw@127.0.0.1:5432/notes
```

Use a disposable database while evaluating the example.

## Plan and apply the migration

The migration planner reads the model in `examples/notes/app.py` and the recorded migration state. It writes numbered SQL and state files for review:

```sh
uv run hinoki migrations --dir examples/notes/migrations plan \
  --models examples.notes.app --name initial
uv run hinoki migrations --dir examples/notes/migrations sql 0001-initial
```

Review the SQL, then apply it explicitly:

```sh
uv run hinoki migrations --dir examples/notes/migrations apply \
  --database "$NOTES_DATABASE__URL"
```

## Start the app and send a request

```sh
uv run hinoki dev examples.notes.main:app
```

The `DatabaseModule` opens a pool for the worker and closes it at shutdown. It does not create tables. In another terminal, create a note and list the collection:

```sh
curl -i -X POST http://127.0.0.1:8000/notes \
  -H 'content-type: application/json' \
  -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The POST returns `201 Created` and a JSON object with the generated `id`, `title` and `body`. The GET returns a JSON array. Invalid input is rejected before the handler runs; see [error handling](ERRORS.md) for the response format.

## Where to go next

- [Tutorial](tutorial/index.md): build the Tracker example in six steps.
- [Models and queries](MODELS.md): fields, querysets and relation loading.
- [Migrations](MIGRATIONS.md): migration files, destructive changes and deployment workflow.
- [API reference](reference/index.md): public symbols and signatures.
