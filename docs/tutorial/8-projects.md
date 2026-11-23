---
status: implemented
related:
  - migrations.md
  - cli.md
  - modules.md
---

# 8. Projects and apps

An app is a package with its own models, migrations and routes, registered in
`hinoki.toml`. This chapter adds a `billing` app to the `notes` project from the
[Quick start](../quick-start.md).

## Create the app

From the project root:

```sh
uv run hinoki app create
```

| Prompt | Default | Answer |
| --- | --- | --- |
| App name | | `billing` |
| URL prefix | `/billing` | `/billing` |
| Depends on | | (none) |

The command writes the package and registers it:

```text
notes/apps/billing/
├── api.py         endpoints of the app
├── routes.py      the list of routes served, mounted at the prefix
├── models.py      tables of the app
└── migrations/    migration history
```

```toml
[[apps]]
name = "billing"
models = "notes.apps.billing.models"
migrations = "notes.apps.billing.migrations"
routes = "notes.apps.billing.routes:routes"
prefix = "/billing"
requires = []
```

Hinoki reads apps only from `hinoki.toml`; it never scans installed packages.
If billing later references a model of another app, list that app in
`requires`: migrations then run in dependency order.

## Add a model

```python
# notes/apps/billing/models.py
"""Models owned by this app."""

from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, pk
from hinoki.utils import utc_now


@db.ordered_by("-created_at")
class Invoice(Model):
    id: UUID = pk(default_factory=uuid4)
    reference: str = field(max_length=40, unique=True)
    paid: bool = False
    created_at: datetime = field(default_factory=utc_now, index=True)
```

The table name is derived: `billing_invoices`, the app followed by the plural
model name. `@db.table("billing_invoices")` pins it, so renaming the class
keeps the physical name; the model rules below ask for it.

## Plan and apply the migration

```sh
uv run hinoki migrations plan billing
uv run hinoki migrations apply
```

```text
database: sqlite:///…/notes/.hinoki/local.sqlite3 (from local SQLite default)
create table billing_invoices
wrote notes/apps/billing/migrations/0001-create-table-billing-invoices.sql
database: sqlite:///…/notes/.hinoki/local.sqlite3 (from local SQLite default)
entries: nothing to apply
billing: 0001-create-table-billing-invoices
```

`plan` compares the models with the recorded migration state and writes the
next migration; the number and the slug come from the change. Review the
`.sql` file before you apply it. With more than one app, `plan` needs the app
name; `apply` runs every app in dependency order.

!!! note "Where the data goes"
    `apply` uses the application's `database` setting, so the variable that
    configures the server, such as `NOTES_DATABASE__URL`, also configures the
    migrations; `plan`, `status` and `apply` print the database they chose. With no
    database configured, `apply` creates `.hinoki/local.sqlite3` beside
    `hinoki.toml`. `HINOKI_DATABASE_URL` overrides the setting for one run.
    If the application cannot be loaded, the commands stop (exit 2) instead of guessing.
    Migrations never run when the application starts.

## Check the project

```sh
uv run hinoki check
uv run pytest
```

```text
8 rule(s): 0 violated, 0 unverifiable, 0 waived
```

`notes/rules.py` keeps two model rules commented out:
`Models.require_explicit_table` and `Models.require_foreign_key_index`.
`hinoki check` imports the models of every `[[apps]]` entry, so a project with
no model at all would report them as `unverifiable`, which fails the check.
Now that billing has `Invoice`, turn them on (and import `Models`):

```python
# notes/rules.py (excerpt)
from hinoki import Actions, Models, Schemas, Transactions

project_rules = RuleSet(
    # …the rules the scaffold wrote…
    Models.require_explicit_table(match="notes.*"),
    Models.require_foreign_key_index(match="notes.*"),
)
```

The check then reports the derived table name:

```text
notes/apps/billing/models.py:11: HNK-DB-001 [violated] Invoice uses the derived table name 'billing_invoices'
  fix: name the table with @db.table(...) or table=... on the model
10 rule(s): 1 violated, 0 unverifiable, 0 waived
```

Add `@db.table("billing_invoices")` above the class (the first line of its
decorators) and the check passes. Pinning the derived name changes no table:
`migrations plan billing` then reports `no changes`.

Endpoints for billing go in `notes/apps/billing/api.py`, and `routes.py` lists
them. The application in `notes/main.py` includes every app's list with
`manifest_routes()`, which mounts this one at `/billing`, so paths inside the
app never repeat the prefix:

```python
# notes/main.py (as hinoki new wrote it)
from hinoki import Hinoki
from hinoki.project import manifest_routes

from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,  # NOTES_* variables and .env
    include=manifest_routes(),  # notes/routes.py, then each app's routes at its prefix
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
)  # the rules come from `rules` in hinoki.toml
```

`include=manifest_routes()` is the explicit form of what `Hinoki()` does by
itself when you leave `include=` out, as the [Tracker](6-run.md) does; the
scaffold writes it so the line shows where routes come from. The same manifest
can also list project-wide `middleware`, an `events` module per app and more than
one rule set (`rules`), as the Tracker's `hinoki.toml` does.

The project's rules (listed in `hinoki.toml`) require every action to name an
`Access` policy with permissions; build one as in [chapter 4](4-endpoints.md).

## Recap

- `hinoki app create` writes an app and registers it in `hinoki.toml`.
- Table names, migration numbers and slugs are derived; you review the SQL.
- The manifest sets each app's prefix; code never repeats it.
