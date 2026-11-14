---
status: implemented
---

# Command line

The `hinoki` command accepts a `module:attribute` target for an application, factory, settings class or model module as documented by each subcommand. Targets are imported from the current project directory.

## Main commands

| Command | Purpose |
| --- | --- |
| `hinoki new NAME` | Create a starter project with settings, rules, tests and agent instructions. |
| `hinoki dev TARGET` | Run an app with reload during development. |
| `hinoki serve TARGET` | Run without reload, using the `server` settings group. |
| `hinoki check TARGET` | Run the application's configured rules; `--rules` and `--package` select rule and source targets. |
| `hinoki settings TARGET` | Show effective settings and their source, with secrets redacted. |
| `hinoki migrations ...` | Plan, inspect and apply database migrations. |
| `hinoki mcp TARGET` | Serve read-only project context to an MCP client over stdio. |

Use `--help` on a command for its current flags. `hinoki mcp` is the development server; application MCP is mounted separately with `expose_as_mcp` and an explicit allowlist.

## Import target examples

```sh
hinoki dev examples.notes.main:app
hinoki check examples.tracker.app:create_app
hinoki settings examples.blog.settings:BlogSettings
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-index
```

When a target import fails, check that the command runs from the project directory, the package has `__init__.py` files where needed, and the attribute name is correct.
