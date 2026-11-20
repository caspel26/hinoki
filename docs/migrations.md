---
status: implemented
related:
  - models.md
  - databases.md
  - cli.md
---

# Migrations

Hinoki compares each app's models with the migration state recorded in its
migration package and writes SQL files you review and commit. Nothing changes a
database until you run `apply`; an application never migrates at startup.

```sh
hinoki migrations plan billing
hinoki migrations apply
```

Commands run from the project, where `hinoki.toml` registers each app's models
and migrations (`hinoki new`, `hinoki app create`). Prefix them with `uv run`
or activate `.venv`.

## Plan a migration

```sh
hinoki migrations plan                  # a project with one app
hinoki migrations plan billing          # one app of several
hinoki migrations plan billing --name add-invoice-index
```

`plan` writes two files into the app's migration package:
`0001-create-table-billing-invoices.sql` and its `.state.json`. The number is
automatic; the slug comes from the first change (`create table
billing_invoices`) unless `--name` replaces it.

In a project with several apps, `plan` needs the app name; `--check` and
`--dry-run` without one cover every app.

!!! warning "Plan for the backend you deploy"
    The SQL uses the dialect of the selected URL: `--database`, then
    `HINOKI_DATABASE_URL`, then local SQLite. Export the production backend's
    URL (a disposable database of the same kind) before planning. The state
    file is backend-neutral.

## Review before applying

Read both files. Changes that can lose data are **destructive** and are not
written unless you pass `--allow-destructive`:

* dropping a table or column, changing a type or constraint, adding a
  non-null column;
* a changed derived table name (renaming the class or moving its module), which
  is planned as a drop and a create. Pin the name with `@db.table(...)`
  ([table names](models.md#table-and-display-names)).

```sh
hinoki migrations plan billing --dry-run
hinoki migrations plan billing --allow-destructive
```

`--dry-run` prints the plan, destructive operations included, and writes
nothing. Changing a constraint on an existing column may need a hand-written
migration.

## Apply and inspect

```sh
export HINOKI_DATABASE_URL="$NOTES_DATABASE__URL"
hinoki migrations status
hinoki migrations sql 0001-create-table-billing-invoices
hinoki migrations apply
hinoki migrations apply billing
```

| Command | Effect |
| --- | --- |
| `status [APP]` | lists applied and pending migrations; creates the ledger if missing |
| `sql NAME` | prints one migration's SQL; add `--app APP` only when the name is ambiguous |
| `apply [APP]` | applies pending migrations of every app, or of `APP` and its prerequisites, in dependency order |

The database is chosen the same way as for `plan`; application settings such
as `NOTES_DATABASE__URL` are not read. The implicit SQLite default
(`sqlite://./.hinoki/local.sqlite3`) is anchored beside `hinoki.toml`, so a
subdirectory uses the same file; an explicit relative SQLite URL stays relative
to the current directory. A connection failure prints `error: database: …`
without the URL and exits `1`. A `--database` URL is visible in the process
list and shell history: do not put a password in it.

The root app keeps unprefixed ledger keys; added apps use namespaced keys, so
`accounts/0001-initial.sql` and `billing/0001-initial.sql` are distinct records.

Run `apply` once per deployment, from one process, never from every worker's
startup ([settings](settings.md#8-database-connection-ownership)). Concurrent
runs are serialized: each file runs in one transaction under a PostgreSQL
advisory lock, a MySQL/MariaDB migration lock or SQLite `BEGIN IMMEDIATE`, and
reads the ledger inside it. On SQLite a second runner waits at most the busy
timeout (5 seconds), then fails as locked.

## Check migration drift

```sh
hinoki migrations plan --check
```

Exits `1` when any app has model changes without a migration file, destructive
ones included, and `0` otherwise. It covers every app in dependency order
(`plan billing --check`: that app and its prerequisites), writes nothing and
opens no database, but imports every registered model and migration module, so
those imports must be safe in CI. `status` compares files with a live
database instead.

## Apps and dependencies

An app that references another app's models declares it in `requires`:

```toml
# hinoki.toml
[[apps]]
name = "billing"
models = "notes.apps.billing.models"
migrations = "notes.apps.billing.migrations"
requires = ["accounts"]
```

`hinoki app create billing` asks for `requires`; `hinoki app add NAME --models
MODULE` registers an existing package. Each app's tables belong only to its
own migration state.

### Cross-app migration safety

* Before planning `billing`, the referenced table and primary key must already
  be recorded in `accounts`' migration state with a compatible type; plan and
  review `accounts` first. It does not need to be applied yet.
* A parent table or referenced column is never dropped while another app's
  recorded state still refers to it, in `plan`, `--dry-run` and `--check`;
  `--allow-destructive` does not override this, and an all-app command fails
  without partial output. Remove and apply the child reference first, then
  plan the parent teardown in a separate deployment.
* `--dry-run` without an app previews every app, possibly against a
  prerequisite's not-yet-planned models: do not apply in that order. It cannot
  take `--name`.

## Backend behaviour

| Backend | Behaviour |
| --- | --- |
| SQLite | rebuilds a table when `ALTER TABLE` cannot express the change safely; holds the pool, suspends foreign-key enforcement on reserved connections, runs `PRAGMA foreign_key_check` before commit and rolls back on invalid references |
| PostgreSQL | each migration runs in a transaction |
| MySQL | DDL commits implicitly: a failed migration may be partially applied (Hinoki says so). Check the schema and `status` before retrying; keep a backup |
| MariaDB | separate backend on the MySQL wire protocol; JSON is a text alias decoded by typed fields |

MySQL and MariaDB foreign keys are table-level constraints. For databases
migrated by an older Hinoki, check `SHOW CREATE TABLE`: MySQL ignores inline
`REFERENCES`, and MariaDB varies by version. Add a missing constraint with a
reviewed manual migration; applied migrations are never rewritten. Automatic
removal of a foreign-key column is refused on these backends.

State files record logical types; files from older versions that recorded
SQLite or MySQL type names are normalized, so switching the planner's backend
is not reported as a type change.

## Without a manifest

A project without `hinoki.toml` passes the models module and directory:

```sh
hinoki migrations --dir app/migrations plan --models app.models
```

These options cannot be combined with a manifest. Prefer adding one: the root
app keeps its ledger keys, so existing history is not replayed.

## See also

- [Configure a database](databases.md)
- [CLI reference](cli.md#migrations)
- [Define models](models.md)
