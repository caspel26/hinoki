# Hinoki example project

This directory is a manifest-backed Hinoki project. [`hinoki.toml`](hinoki.toml)
registers Notes, Tracker and Blog as separate apps, each with its own model
module and migration package. From this directory, inspect and manage them
with the project commands (the repository's locked environment lives one level
up):

```sh
uv run --project .. hinoki app list
uv run --project .. hinoki migrations plan --app notes
uv run --project .. hinoki migrations apply
uv run --project .. hinoki manage greet Peppe
uv run --project .. hinoki shell
```

With no database URL, migration commands and app settings use the local SQLite
file `.hinoki/local.sqlite3` inside `examples/`. `hinoki migrations apply` initializes every
registered app in dependency order; use `--app NAME` to limit the operation to
one app and its prerequisites. Migration numbers and slugs are generated from
the planned schema changes. Review generated SQL and state before applying.

Use `hinoki new NAME` to scaffold a separate project and `hinoki app create
NAME` from a manifest-backed project to add an independently registered app.
See each app's README for its API and configuration: [Notes](notes/README.md),
[Tracker](tracker/README.md), and [Blog](blog/README.md).
