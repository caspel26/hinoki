---
status: implemented
---

# Command line

The `hinoki` command accepts a `module:attribute` target for an application, factory, settings class or model module as documented by each subcommand. Targets are imported from the current project directory.

## Commands and options

| Command | Purpose |
| --- | --- |
| `hinoki new NAME` | Create a starter project with settings, rules, tests and agent instructions. |
| `hinoki dev TARGET` | Run an app with reload during development. |
| `hinoki serve TARGET` | Run without reload, using the `server` settings group. |
| `hinoki check TARGET` | Run the application's configured rules; `--rules` and `--package` select rule and source targets. |
| `hinoki settings TARGET` | Show effective settings and their source, with secrets redacted. `--dotenv` and `--toml` select optional files. |
| `hinoki migrations ...` | Plan, inspect and apply database migrations. |
| `hinoki manage TARGET COMMAND` | Run an app-defined command with its declared module resources. |
| `hinoki mcp TARGET` | Serve project context to an MCP client over stdio. Scenario tools are off by default. |

### `new`

```sh
hinoki new notes --dir .
```

`NAME` is a lowercase package name containing letters and digits. `--dir` chooses the parent directory and defaults to the current directory.

### `dev` and `serve`

```sh
hinoki dev examples.notes.main:app --host 127.0.0.1 --port 8000
hinoki serve examples.notes.main:app --workers 4
```

Both commands accept `--host` and `--port`; they override `settings.server.host` and `.port`. `serve` also accepts `--workers`, overriding `.workers`. Otherwise values come from the app's `FrameworkSettings.server` group. `dev` enables reload and runs one worker. Granian is provided by the optional server extra.

### `check`

```sh
hinoki check examples.tracker.app:create_app \
  --package examples.tracker --phase startup --format text
```

Options:

* `--rules MODULE:ATTRIBUTE` selects a `RuleSet`; the app's rules are used by default.
* `--package PACKAGE` selects a package for static import checks. Repeat it for multiple packages. Without it, the package containing the target is scanned.
* `--phase startup|static` selects a phase; the default runs both.
* `--waivers PATH` selects a waiver file; the default is `hinoki-rules.toml` in the current directory.
* `--format text|json` selects human-readable or versioned JSON output.
* `--allow-effects` also runs checks declared as effectful, which may access files, network or databases.

Exit status is `0` when no rules fail, `1` for violations, and `2` when the target or checks cannot run.

### `settings`

```sh
hinoki settings examples.blog.settings:BlogSettings \
  --prefix BLOG_ --dotenv .env --toml settings.toml --format json
hinoki settings examples.blog.settings:BlogSettings --example > .env.example
```

`TARGET` can be a settings class or an app with typed settings. `--prefix`, `--dotenv` and `--toml` override sources declared by the settings class. `--format text|json` selects output; `--example` prints a `.env.example` with defaults and types, leaving secrets and required fields blank. Values of `Secret` are redacted.

### `manage`

```sh
hinoki manage examples.management:app --help
hinoki manage examples.management:app greet Peppe
```

Commands are declared on the app with `Hinoki(commands=[...])`. Each command subclasses `hinoki.management.Command`, gives itself a lower-case name, and adds arguments through `add_arguments(parser)`. `--help` after the app target lists its commands; `--help` after a command shows that command's arguments.

```python
from argparse import ArgumentParser, Namespace

from hinoki import Hinoki
from hinoki.management import Command, CommandContext


class Greet(Command):
    name = "greet"
    help = "print a greeting"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("name", nargs="?", default="world")

    def handle(self, _context: CommandContext, args: Namespace) -> None:
        print(f"Hello, {args.name}!")


app = Hinoki(commands=[Greet])
```

The same example is in `examples/management.py` and can be run from the repository root.

`handle(context, args)` may be synchronous or asynchronous. A sync handler runs on Hinoki's bounded executor. It can read `context.settings` and retrieve resources with `context.get(Type)` or `context.find(Type)`. Declare module classes in the command's `requires` tuple; Hinoki starts those modules and their dependencies for the command, then closes them before it exits. Other app modules are not started. A command with no requirements starts no app modules.

Commands run inside the project process and can modify data. Use disposable databases when developing or testing a command that writes. A handler returns `None` for success or an exit code from 0 through 255. Unknown commands and invalid arguments exit 2; a command failure exits nonzero. App settings load when Hinoki constructs the app, but module resources open only for the selected command.

### `migrations`

`--dir` belongs immediately after `migrations` and defaults to `migrations/`:

```sh
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-index --dry-run
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-index --allow-destructive
hinoki migrations --dir examples/blog/migrations status --database "$BLOG_DATABASE__URL"
hinoki migrations --dir examples/blog/migrations apply --database "$BLOG_DATABASE__URL"
hinoki migrations --dir examples/blog/migrations sql 0001-initial
```

`plan` requires `--models MODULE`; `--name` defaults to `changes`. `--dry-run` prints operations without writing files. Destructive operations require `--allow-destructive`. `status` and `apply` accept `--database URL`, defaulting to `HINOKI_DATABASE_URL`. `sql NAME` prints a migration's SQL. `status` and `apply` connect to PostgreSQL; use a disposable database while reviewing migrations.

### `mcp`

```sh
hinoki mcp examples.tracker.app:create_app \
  --rules examples.tracker.rules:tracker_rules \
  --package examples.tracker --models examples.tracker.models
```

The development server uses MCP over stdio. It accepts `--rules`, repeatable `--package`, repeatable `--models`, and `--waivers` with the same target meanings as `check`. `--scenarios off|read|write` controls whether `run_scenario` is registered: `read` allows GET, HEAD and OPTIONS; `write` allows every supported method. Scenarios start the real app, run handlers and lifespan hooks, and may write data even for a GET. Keep the tool server owner-controlled and point the app at a disposable database.

The development MCP is separate from application MCP, which is mounted with `expose_as_mcp` and an explicit action allowlist. See the [development MCP guide](dev-mcp.md) for tool inputs and results.

## Import target examples

```sh
hinoki dev examples.notes.main:app
hinoki check examples.tracker.app:create_app
hinoki settings examples.blog.settings:BlogSettings
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-index --dry-run
```

When a target import fails, check that the command runs from the project directory, the package has `__init__.py` files where needed, and the attribute name is correct.
