# Quick start

This walkthrough uses the default local SQLite database, so no database server
or URL setup is needed. It creates the table with an explicit migration;
Hinoki does not create or migrate tables when the app starts. To use PostgreSQL,
MySQL or MariaDB instead, see [Configure a database](databases.md).

## Plan and apply the migration

`examples/hinoki.toml` registers Notes, Tracker and Blog as independent apps.
From the repository root, enter `examples/` and use the locked environment in
the parent directory. The migration planner reads the Notes model and its
recorded state and writes numbered SQL and state files for review:

```sh
cd examples
uv run --project .. hinoki app list
uv run --project .. hinoki migrations plan --app notes
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes
```

Review the SQL, then apply it explicitly. With no URL set, the migration
command and the Notes app both use `sqlite://./.hinoki/local.sqlite3`:

```sh
uv run --project .. hinoki migrations apply --app notes
```

The local database file and migration ledger are created when `apply` runs.
Use a disposable database while evaluating the example.

## Start the app and send a request

```sh
uv run --project .. hinoki dev notes.main:app
```

The `database` field of the Notes settings opens a pool for the worker and closes it at shutdown. It does not create tables. In another terminal, create a note and list the collection:

```sh
curl -i -X POST http://127.0.0.1:8000/notes \
  -H 'content-type: application/json' \
  -d '{"title":"First note","body":"Hello"}'
curl http://127.0.0.1:8000/notes
```

The POST returns `201 Created` and a JSON object with the generated `id`, `title` and `body`. The GET returns a JSON array. Invalid input is rejected before the handler runs; see [error handling](errors.md) for the response format.

## Where to go next

- [Tutorial](tutorial/index.md): build the Tracker example in seven steps, then learn the project and app workflow.
- [Models and queries](models.md): fields, querysets and relation loading.
- [Migrations](migrations.md): migration files, destructive changes and deployment workflow.
- [Configure a database](databases.md): choose another backend, set its URL and understand backend-specific behavior.
- [API reference](reference/index.md): public symbols and signatures.
