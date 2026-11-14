---
status: implemented
related:
  - models.md
  - cli.md
---

# Migrations

Hinoki compares the models in a module with the migration state recorded in files. It can write a migration plan, but never applies schema changes when an application starts.

## Create a migration

```sh
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-published
```

The planner targets the local SQLite default unless a URL is provided through `HINOKI_DATABASE_URL` or `--database`. It writes a numbered `.sql` file and a matching `.state.json` file. Review both before applying the change. To inspect a plan without writing files, add `--dry-run`.

## Apply and inspect

```sh
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
hinoki migrations --dir examples/blog/migrations status
hinoki migrations --dir examples/blog/migrations sql 0001-initial
hinoki migrations --dir examples/blog/migrations apply
```

With no URL, `status` and `apply` use `sqlite://./.hinoki/local.sqlite3`. Set `HINOKI_DATABASE_URL` or use `--database URL` to target another supported backend. A URL argument is visible in the process list and shell history, so do not pass a URL containing a password that way. A failed connection is reported as `error: database: …` with exit code 1, without the URL.

Use a disposable database to review a migration before running it against important data.

SQLite uses table rebuilds when a schema change cannot be expressed safely with `ALTER TABLE`. Review the generated SQL carefully. Dropping columns, changing types or constraints, and adding non-null columns can be destructive and require `--allow-destructive`. Hinoki holds the SQLite pool during a migration, suspends foreign-key enforcement on reserved connections, runs `PRAGMA foreign_key_check` before commit, and rolls the migration back if references are invalid.

## Destructive changes

Operations that can remove data are reported as destructive and are not written unless `--allow-destructive` is passed to `plan`. Read the generated migration before opting in. Changing a constraint on an existing column may require a hand-written migration.

Migrations are files committed with the application. Do not run them from every server worker's startup hook. For the connection lifecycle, see [settings](settings.md#8-database-connection-ownership).
