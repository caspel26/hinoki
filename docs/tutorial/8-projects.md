---
status: implemented
---

# 8. Create a project and modular app

The Tracker steps above use an existing example with checked-in migrations. For a new project, `hinoki new` creates the `hinoki.toml` manifest and registers its first app. Each additional app owns its model and migration modules.

```sh
uv run --locked hinoki new notes
cd notes
uv sync
```

The generated manifest registers `notes.models` and `notes.migrations`. The scaffold also includes a health endpoint, typed settings, rules, a test, and agent instructions. The manifest also names the project's list of routes (`routes = "notes.routes:routes"`): `main.py` has no `include=`, because the application that `hinoki.toml` names binds those routes itself. Hinoki does not scan installed packages for apps.

Add an independently organized app:

```sh
uv run hinoki app create billing
```

This creates `notes/apps/billing/models.py`, `notes/apps/billing/routes.py` (an empty `routes` list) and `notes/apps/billing/migrations/`, then adds the module paths to `hinoki.toml`, including `routes` and `prefix = "/billing"`: whatever you list in `billing/routes.py` is served under `/billing`. If billing references a model from another app, add that app to billing's `requires` list so Hinoki can validate and order migrations.

Add a model to the generated `notes/apps/billing/models.py` (or the app's declared models module):

```python
from uuid import UUID, uuid4

from hinoki.orm import Model, field


class Invoice(Model):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    reference: str = field(max_length=40, unique=True)
```

Plan its migration from the project root. The app name is required when more than one app is registered:

```sh
uv run hinoki migrations plan --app billing --dry-run
uv run hinoki migrations plan --app billing
uv run hinoki migrations status
uv run hinoki migrations sql --app billing 0001-create-table-billing-invoices
uv run hinoki migrations apply
```

Hinoki assigns the next migration number and derives a stable slug from the first schema change, such as `create-table-billing-invoices` (the table is `billing_invoices`: the app, then the model). You can optionally override just the slug with `--name add-invoice-table`. Review the generated SQL and state file before applying it. With no database URL configured, `status` and `apply` initialize the local SQLite database at `.hinoki/local.sqlite3`. Set `HINOKI_DATABASE_URL` to target another supported backend. Migrations are explicit commands and never run automatically when the app starts.

See [project migrations](../migrations.md) for dependency ordering, destructive changes and existing projects that still use explicit `--models` and `--dir` targets.
