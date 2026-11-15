---
status: implemented
related:
  - models.md
  - cli.md
---

# Migrations

Hinoki compares each registered app's models with that app's migration state recorded in files. It can write a migration plan, but never applies schema changes when an application starts. `hinoki new` creates an explicit `hinoki.toml`; `hinoki app create NAME` adds another app and its migration package.

## Create a migration

```sh
uv run hinoki migrations plan --name add-published
uv run hinoki migrations plan --app billing --name add-invoice-index
```

The manifest must register the selected app. A single-app project defaults to that app; in a multi-app project, `plan` requires `--app`. The planner targets the local SQLite default unless a URL is provided through `HINOKI_DATABASE_URL` or `--database`. It writes a numbered `.sql` file and a matching `.state.json` file into the registered migration package. Numbering and a stable slug derived from the first schema change are automatic; an optional `--name` replaces only that slug. Review both files before applying the change. To inspect a plan without writing files, add `--dry-run`. Cross-app model references require an explicit `requires` dependency in `hinoki.toml`; this lets Hinoki validate references and order migrations without assigning another app's tables to the current app's migration state.

The state file stores logical column types that are stable across backends; the
generated SQL uses the selected backend's types. Existing state files that
recorded SQLite or MySQL-specific type names are normalized when planning, so
switching the planner's backend does not appear as a destructive type change.

## Check migration drift

To check that model changes have a committed migration file in CI, run:

```sh
uv run hinoki migrations plan --check
```

It exits `1` when operations are pending, including destructive operations,
and `0` when the model state matches the latest migration state file. It does
not write migration files or connect to a database. It imports the selected
app's migration module and every registered app's model module (cross-app
foreign keys require the other models for validation), so those imports must
be safe for a CI check. In a multi-app project, pass `--app NAME` and run the
check once for each registered app; without a selection, it asks for `--app`
just like `plan`. This check concerns file state only. Use `migrations status`
to compare the files with a live database's applied migration ledger.
`--dry-run` remains a preview that prints operations and exits successfully
when changes are pending.

For example, `billing` can depend on `accounts`:

```toml
[[apps]]
name = "billing"
models = "sample.apps.billing.models"
migrations = "sample.apps.billing.migrations"
requires = ["accounts"]
```

## Apply and inspect

```sh
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
uv run hinoki migrations status
uv run hinoki migrations sql --app accounts 0001-initial
uv run hinoki migrations apply
```

With no URL, `status` and `apply` use `sqlite://./.hinoki/local.sqlite3` and create the migration ledger/database as needed. Both operate on every registered app in dependency order. `--app NAME` limits them to that app and its prerequisites. `sql` requires `--app` in a multi-app project. The scaffolded root app keeps the traditional unprefixed migration ledger keys, so a project can add a manifest without replaying its existing migration history; added apps use namespaced keys, so `accounts/0001-initial.sql` and `billing/0001-initial.sql` are distinct records. Set `HINOKI_DATABASE_URL` or use `--database URL` to target another supported backend. A URL argument is visible in the process list and shell history, so do not pass a URL containing a password that way. A failed connection is reported as `error: database: …` with exit code 1, without the URL.

Existing projects without a `hinoki.toml` can continue using explicit `--models MODULE` and `--dir PATH` options. These options cannot be combined with a project manifest; use the manifest as the single source for registered apps.

Use a disposable database to review a migration before running it against important data.

SQLite uses table rebuilds when a schema change cannot be expressed safely with `ALTER TABLE`. Review the generated SQL carefully. Dropping columns, changing types or constraints, and adding non-null columns can be destructive and require `--allow-destructive`. Hinoki holds the SQLite pool during a migration, suspends foreign-key enforcement on reserved connections, runs `PRAGMA foreign_key_check` before commit, and rolls the migration back if references are invalid.

MySQL and MariaDB migrations declare foreign keys at table level. For databases
migrated with an older Hinoki version, inspect `SHOW CREATE TABLE`: MySQL ignores
inline `REFERENCES` clauses, while MariaDB behavior varies by server version.
Add any missing constraint through a reviewed manual migration; applied
migrations are never rewritten automatically. Automatic removal of a foreign
key column is refused for these backends because the planner cannot know
whether an older migration actually created its constraint.

## Destructive changes

Operations that can remove data are reported as destructive and are not written unless `--allow-destructive` is passed to `plan`. Read the generated migration before opting in. Changing a constraint on an existing column may require a hand-written migration.

Migrations are files committed with the application. Do not run them from every server worker's startup hook. For the connection lifecycle, see [settings](settings.md#8-database-connection-ownership).
