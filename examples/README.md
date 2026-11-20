# Hinoki examples

Three applications that share one project. [`hinoki.toml`](hinoki.toml)
registers each one as an app with its own models and migrations, plus the
admin's user tables.

| Example | Shows | Application |
| --- | --- | --- |
| [Notes](notes/README.md) | the smallest app: a model, schemas, four routes | `notes.main:app` |
| [Tracker](tracker/README.md) | layers, bearer access, rules, an MCP tool | `tracker.app:create_app` |
| [Blog](blog/README.md) | generated CRUD, relations, login, the admin | `blog.main:create_app` |
| `project/` | a management command | `project.app:app` (the manifest's application) |

## Run

From this directory; `uv` finds the repository's environment one level up.

```sh
uv run hinoki app list
uv run hinoki migrations apply
uv run hinoki dev notes.main:app
```

`migrations apply` creates `.hinoki/local.sqlite3` here and applies every app in
dependency order; `uv run hinoki migrations apply notes` limits it to one app and
its prerequisites. `uv run hinoki manage greet Ada` runs the management command,
and `uv run hinoki shell` opens a shell with the registered models.

<!-- post-G1: the last sentence of the note is true only after G1 -->
> [!NOTE]
> Server and check commands name their application here because this
> directory hosts several. In a project created with `hinoki new`,
> `hinoki.toml` names the application and the commands take no target.

## How the examples are built

Notes declares `app` at module level and lists its routes with `include=`,
because it is not the manifest's application. Tracker and Blog use a factory
(`create_app`): construction takes a database URL or secrets, and the tests
build several applications. A project created with `hinoki new` lists its
routes in `routes.py` instead, and `hinoki.toml` names that list.
