# Hinoki example project

This directory's apps are registered by the repository root `hinoki.toml`:
Notes, Tracker and Blog each own a model module and migration package. From the
repository root, inspect and manage them with the project commands:

```sh
uv run hinoki app list
uv run hinoki migrations plan --app notes
uv run hinoki migrations apply
uv run hinoki manage greet Peppe
uv run hinoki shell
```

With no database URL, migration commands and app settings use the local SQLite
file `.hinoki/local.sqlite3`. `hinoki migrations apply` initializes every
registered app in dependency order; use `--app NAME` to limit the operation to
one app and its prerequisites. Migration numbers and slugs are generated from
the planned schema changes. Review generated SQL and state before applying.

Use `hinoki new NAME` to scaffold a separate project and `hinoki app create
NAME` from a manifest-backed project to add an independently registered app.
See each app's README for its API and configuration: [Notes](notes/README.md),
[Tracker](tracker/README.md), and [Blog](blog/README.md).
