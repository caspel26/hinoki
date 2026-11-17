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

`hinoki makemigrations` is the short form of `hinoki migrations plan`: it takes the app as an optional argument (`hinoki makemigrations billing`) and the same `--name`, `--dry-run`, `--check`, `--allow-destructive` and `--database` options. It requires a project manifest; the legacy `--models` and `--dir` options stay on `hinoki migrations plan`.

The manifest must register the selected app. A single-app project defaults to that app; in a multi-app project, writing a plan requires `--app`. Read-only `--dry-run` and `--check` without `--app` cover every registered app in dependency order. The planner targets the local SQLite default unless a URL is provided through `HINOKI_DATABASE_URL` or `--database`. It writes a numbered `.sql` file and a matching `.state.json` file into the registered migration package. Numbering and a stable slug derived from the first schema change are automatic; an optional `--name` replaces only that slug for a focused plan. Review both files before applying the change. Cross-app model references require an explicit `requires` dependency in `hinoki.toml`; this lets Hinoki validate references and order migrations without assigning another app's tables to the current app's migration state.

`--dry-run` previews ordinary destructive changes without `--allow-destructive`
and writes no files. An all-app preview is hypothetical: it can show a child
app change against a prerequisite's current candidate models before that
prerequisite has a recorded migration. Do not treat displayed plans as ready to
apply in that order. An all-app preview cannot use `--name`; select one app to
preview a named migration.

### Cross-app migration safety

Before writing a focused app plan, Hinoki checks cross-app foreign keys against
the prerequisite app's recorded migration state. The referenced table and
column must already be recorded, the column must be a primary key, and its
logical type must be compatible. If the prerequisite has not been planned yet,
plan and review it first. The prerequisite migration does not need to have been
applied to the database before the dependent app can be planned.

Hinoki also refuses to plan a parent table or referenced-column teardown while
any recorded state snapshot of another app still refers to it. This guard
applies to focused/all-app plans, `--dry-run`, and `--check`, and cannot be
overridden with `--allow-destructive`. An all-app command fails without
printing partial diffs. This prevents a generated parent drop from preceding a
child foreign-key removal. Perform cross-app teardowns as reviewed, separately
deployed phases: remove and apply the child reference first, then prepare and
apply the parent teardown. The planner does not infer live database safety
from migration files.

The state file stores logical column types that are stable across backends; the
generated SQL uses the selected backend's types. Existing state files that
recorded SQLite or MySQL-specific type names are normalized when planning, so
switching the planner's backend does not appear as a destructive type change.

New `Model` classes may omit `table=`; Hinoki derives a plural snake-case table
name from the Python class. Keep `table=` explicit for models with existing
migration history so later Python class renames do not change physical table
identity. A changed implicit name appears as a drop/create plan and the
destructive guard still applies. Hinoki refuses generated names reserved for
its migration ledger or SQLite internals. See [Models and queries](models.md#define-a-model)
for display-name overrides and the fallback naming rules.

## Check migration drift

To check that model changes have a migration file in CI, run:

```sh
uv run hinoki migrations plan --check
```

It exits `1` when operations are pending, including destructive operations,
and `0` when every selected app matches its latest migration state file. It
does not write migration files or connect to a database. It imports registered
model and migration modules (cross-app foreign keys require every model module),
so those imports must be safe in CI. In a multi-app project, no `--app` checks
every app in dependency order; `--app NAME` checks one app and its
prerequisites. This check concerns file state only. Use `migrations status` to
compare files with a live database's applied migration ledger. `--dry-run`
without `--app` previews every registered app and exits successfully when
changes are pending.

For example, `billing` can depend on `accounts`:

```toml
[[apps]]
name = "billing"
models = "sample.apps.billing.models"
migrations = "sample.apps.billing.migrations"
requires = ["accounts"]
```

## Apply and inspect

`hinoki migrate` is the short form of `hinoki migrations apply`; `hinoki migrate billing` applies `billing` and its prerequisites. Generating and applying stay two separate commands, so a migration is never written and applied in one step.

```sh
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
uv run hinoki migrations status
uv run hinoki migrations sql --app accounts 0001-initial
uv run hinoki migrations apply
```

With no URL, `status` and `apply` use `sqlite://./.hinoki/local.sqlite3` and create the migration ledger/database as needed. In a manifest project this implicit path is anchored at the directory containing `hinoki.toml`, even when the command runs from a subdirectory. Without a manifest it stays relative to the current working directory. An explicit relative SQLite URL from `HINOKI_DATABASE_URL` or `--database` remains relative to the current working directory. Both commands operate on every registered app in dependency order. `--app NAME` limits them to that app and its prerequisites. `sql` requires `--app` in a multi-app project. The scaffolded root app keeps the traditional unprefixed migration ledger keys, so a project can add a manifest without replaying its existing migration history; added apps use namespaced keys, so `accounts/0001-initial.sql` and `billing/0001-initial.sql` are distinct records. Set `HINOKI_DATABASE_URL` or use `--database URL` to target another supported backend. A URL argument is visible in the process list and shell history, so do not pass a URL containing a password that way. A failed connection is reported as `error: database: …` with exit code 1, without the URL.

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
