---
status: implemented
---

# Interactive shell

Use `hinoki shell` to inspect application state and run ORM queries from an
interactive Python prompt:

```sh
uv run hinoki shell
```

The project manifest supplies the application target and model modules. To
open an app outside a project manifest, pass its `module:attribute` target and
model modules explicitly:

```sh
uv run hinoki shell examples.blog.main:app --models examples.blog.models
```

The shell exposes `app`, its validated `settings`, and concrete model classes
from registered modules. It supports multiline statements and top-level
`await` on one persistent event loop:

```pycon
>>> notes = await Note.all()
>>> len(notes)
3
>>> app.title
'Hinoki Blog'
```

Use the async ORM methods with `await` in this shell. Synchronous ORM methods
are intended for synchronous handlers and cannot block the shell's event loop.

Each installed app module is started in dependency order and closed when the
session ends. Application `on_startup` and `on_shutdown` hooks are not called.
This avoids unrelated actions such as sending mail or seeding data. Hinoki
does not apply migrations; run `hinoki migrations apply` explicitly when
needed. The shell requires a terminal. Ctrl-D exits, Ctrl-C at the prompt exits,
and Ctrl-C during a cell interrupts that cell.
Unmanaged asyncio tasks are cancelled on exit; cleanup is bounded by the app's
`shutdown_timeout`. A task that ignores cancellation is abandoned when that
deadline expires. Startup is bounded too; if a module suppresses cancellation
indefinitely while opening, the shell stops the loop and cannot guarantee that
the partially started module released its own resources.

Python entered at the prompt runs with the current user's privileges. Database
operations use the configured database and may change its contents; use a
development database while exploring.

See the [CLI reference](cli.md#shell) for target selection and error behavior.
