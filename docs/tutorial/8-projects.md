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
├── models.py      tables of the app
├── routes.py      endpoints, mounted at the prefix
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
model name. Pin it with `@db.table(...)` only to keep a physical name stable,
for example when you rename the class.

## Plan and apply the migration

```sh
uv run hinoki migrations plan billing
uv run hinoki migrations apply
```

```text
database: sqlite:///…/notes/.hinoki/local.sqlite3 (from local SQLite default)
create table billing_invoices
wrote …/notes/apps/billing/migrations/0001-create-table-billing-invoices.sql
database: sqlite:///…/notes/.hinoki/local.sqlite3 (from local SQLite default)
notes: nothing to apply
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

Endpoints for billing go in the app, and `notes/apps/billing/routes.py` lists
them. The application in `notes/main.py` includes every app's list with
`manifest_routes()`, which mounts this one at `/billing`, so paths inside the
app never repeat the prefix:

```python
# notes/main.py (as hinoki new wrote it)
from hinoki import Hinoki
from hinoki.project import manifest_routes

from notes.rules import project_rules
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,  # NOTES_* variables and .env
    include=manifest_routes(),  # notes/routes.py, then each app's routes at its prefix
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    rules=project_rules,
)
```

The project's rules require every action to name an
`Access` policy with permissions; build one as in [chapter 4](4-endpoints.md).

## Recap

- `hinoki app create` writes an app and registers it in `hinoki.toml`.
- Table names, migration numbers and slugs are derived; you review the SQL.
- The manifest sets each app's prefix; code never repeats it.
