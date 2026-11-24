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
    The SQL uses the dialect of the [selected database](#which-database):
    usually the application's own `database` setting. Point it at the
    production backend (a disposable database of the same kind) before
    planning. The state file is backend-neutral.

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
hinoki migrations status
hinoki migrations sql 0001-create-table-billing-invoices
hinoki migrations apply
hinoki migrations apply billing
```

| Command | Effect |
| --- | --- |
| `status [APP]` | lists applied and pending migrations (nothing when there are none); writes nothing (connecting still creates a missing SQLite file): a database without the ledger has none applied, and `apply` creates it |
| `sql NAME` | prints one migration's SQL; add `--app APP` only when the name is ambiguous |
| `apply [APP]` | applies pending migrations of every app, or of `APP` and its prerequisites, in dependency order |

### Which database

`plan`, `status` and `apply` use the first of:

1. `--database URL`;
2. the `HINOKI_DATABASE_URL` environment variable;
3. the project's application (`project.application` in `hinoki.toml`): a
   `DatabaseModule` it passes in `modules=` or installs with `app.install(...)`,
   otherwise its `database` setting, read from its settings sources as the
   server reads them, for example `NOTES_DATABASE__URL` or `.env`;
4. local SQLite, `sqlite://./.hinoki/local.sqlite3`.

The application is imported from the project root to read its settings, so
`.env` and other relative settings files are the ones beside `hinoki.toml`,
even from a subdirectory. Anything it prints while importing goes to standard
error. No connection opens until the command needs one, and nothing is
started. The command never falls back to another database: it stops with exit
`2` when

- the application cannot be imported (the message names the missing module or
  attribute) or its settings cannot be loaded;
- it installs a database module of its own whose URL Hinoki cannot read;
- the application's database is in-memory SQLite (`sqlite://:memory:`), where
  nothing it migrates would survive. A `--database sqlite://:memory:` URL is
  refused with exit `1` instead, because SQLite needs a file URL there.

Fix the application, or choose the database with `--database` or
`HINOKI_DATABASE_URL`, which skip step 3 and never import the application. An
application without a database still migrates the local SQLite default, but
the server has none to use: declare `database: settings.DatabaseSettings`
([configure a database](databases.md#set-up-a-database-in-four-steps)).
Outside a project (`--models`/`--dir`), step 3 is skipped.

`plan`, `status` and `apply` print the chosen database and its source on
standard error. Everything before the host (user and password) and the URL
options are masked, and a URL that does not parse is not echoed:

```text
$ NOTES_DATABASE__URL=postgresql://notes:secret@db.internal/notes hinoki migrations status
database: postgresql://***@db.internal/notes (from settings: ProjectSettings.database)
```

A relative SQLite path from the settings or the default is anchored beside
`hinoki.toml`, so migrations run from a subdirectory use the same file; a
relative SQLite URL given with `--database` or `HINOKI_DATABASE_URL` stays
relative to the current directory. The server and `hinoki settings` do not
anchor: they resolve the path, and read `.env`, from the directory they start
in, so start them from the project root. A connection failure prints
`error: database: …` and exits `1`. A
`--database` URL is visible in the process list and shell history: do not put
a password in it.

An older scaffold's root app keeps unprefixed ledger keys; apps from `hinoki app create` use namespaced keys, so
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
ones included, `0` otherwise, and `2` when the database whose dialect it plans
for cannot be resolved ([which database](#which-database)). It covers every
app in dependency order (`plan billing --check`: that app and its
prerequisites), writes nothing and opens no connection. It imports every
registered model and migration module and, unless `--database` or
`HINOKI_DATABASE_URL` names the database, the project's application to read
its database, so those imports must be safe in CI:

```sh
HINOKI_DATABASE_URL=postgresql://ci@localhost/notes hinoki migrations plan --check
```

`status` compares files with a live database instead.

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
MODULE` registers an existing package. Hinoki ships two such apps whose tables
are optional: `admin` (`hinoki.admin.users.models`, see the [admin](admin.md#create-the-admins-tables))
and `ratelimit` (`hinoki.ratelimit_store.models`, the `hinoki_rate_limits` table of
[`RateLimit`](endpoints.md#rate-limit-an-action)). Neither is applied unless you
register it. Each app's tables belong only to its
own migration state. The Tracker example has three such apps
([`hinoki.toml` lines 14-27](https://github.com/caspel26/hinoki/blob/main/examples/tracker/hinoki.toml)):
`tasks` requires `members`, and `activity` requires both, so `apply` runs them
in that order.

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

## Migration compatibility

For the planned `0.1.x` line, Hinoki keeps migration files generated by
`0.1.0` readable and applicable across patch releases. New state sidecars carry
`"format_version": 1`; recognized legacy sidecars created before that marker
are treated as version 1, including the historical omission of the `indexes`
list. Hinoki rejects malformed state and unknown format versions before
writing a plan.
Restore damaged files from version control; Hinoki does not rewrite them
automatically.

Once a SQL migration has been applied, keep its exact contents unchanged. The
database ledger records a SHA-256 checksum of the SQL file, and `status` and
`apply` report an error if an applied file has changed or disappeared. The
checksum does not cover the state sidecar and is not a signature. `apply`
checks and executes SQL files; it does not read state sidecars. `plan` reads
and validates sidecars. Files that do not match Hinoki's numbered migration
name pattern are not treated as migrations. Review and back up both files, test
migrations against disposable data, and apply them explicitly. SQL transaction
and rollback behavior depends on the backend; MySQL and MariaDB may leave DDL
partially applied after a failure.

Hinoki may make documented Python, CLI, settings, runtime or migration-format
breaks in a later `0.y.0` while it is pre-1.0. Such changes must appear in
release notes; if a later format stops reading version 1, the release must
document an upgrade path. No fixed deprecation period or support for old
framework versions is promised.

## Bridge tables of many-to-many relations

A [`many_to_many`](relations.md#many-to-many) field adds its bridge model to the models module, right after its owner, so `plan` creates the bridge table in the same migration as the owner: the two foreign keys with `ON DELETE` rules, a unique pair and an index on the target column. Renaming the owner with `@db.table` before its first migration names the generated bridge `<owner table>_<field>` too. The planner does not plan table renames: changing the table of an already migrated owner drops the old bridge table and creates a new one (a destructive change `plan` refuses unless allowed), so move the data with a reviewed manual migration. An adopted bridge (`bridge="NoteLabel"`) is an ordinary model and migrates like one. Nothing runs against the database until you apply the migration.

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
