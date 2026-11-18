# Hinoki example project

This directory is a manifest-backed Hinoki project. [`hinoki.toml`](hinoki.toml)
registers Notes, Tracker and Blog as separate apps, each with its own model
module and migration package. From this directory, inspect and manage them
with the project commands (the repository's locked environment lives one level
up):

```sh
uv run --project .. hinoki app list
uv run --project .. hinoki migrations plan --app notes
uv run --project .. hinoki migrations sql --app notes 0001-create-table-notes
uv run --project .. hinoki migrations apply --app notes
uv run --project .. hinoki manage greet Peppe
uv run --project .. hinoki shell
```

With no database URL, migration commands and app settings use the local SQLite
file `.hinoki/local.sqlite3` inside `examples/`. `hinoki migrations apply` initializes every
registered app in dependency order; use `--app NAME` to limit the operation to
one app and its prerequisites. With no `--app`, apply initializes all three
registered apps in dependency order. Migration numbers and slugs are generated
from planned schema changes. Review generated SQL and state before applying.

Use `hinoki new NAME` to scaffold a separate project and `hinoki app create
NAME` from a manifest-backed project to add an independently registered app (`hinoki app add NAME --models
MODULE` registers a package that already exists). A
scaffolded project lists its routes in `routes.py`, which `hinoki.toml` names; the
examples here keep their explicit `include=[...]`, which always takes precedence
over the manifest. Notes declares `app` at module level; Tracker and Blog use a
factory (`create_app`) because their construction takes a database URL or reads
secrets, and the tests build several applications.
See each app's README for its API and configuration: [Notes](notes/README.md),
[Tracker](tracker/README.md), and [Blog](blog/README.md).
