---
status: implemented
related:
  - cli.md
  - models.md
  - modules.md
---

# Explore an application in the shell

`hinoki shell` opens Python with your application loaded and its database
connected, so you can try queries against real data:

```sh
uv run hinoki shell
```

```pycon
>>> await Invoice.filter(paid=False).acount()
3
>>> [invoice.reference for invoice in await Invoice.all()]
['INV-003', 'INV-002', 'INV-001']
>>> settings.database.url
Secret('***')
```

## What is in the namespace

| Name | Value |
| --- | --- |
| `app` | the application from `hinoki.toml` (`[project] application`) |
| `settings` | its validated settings |
| model classes | every concrete model of every registered app, by class name (`Invoice`, `Note`, …) |

Two models with the same class name in different apps stop the shell before the
prompt; select the modules you need with `--models`. A model may not be called
`app` or `settings`.

## Await at the prompt

The shell keeps one event loop for the session, so top-level `await` and
multiline statements work. Use the async ORM methods (`acount()`, `aget()`,
awaiting a query): the synchronous ones are meant for `def` handlers and cannot
block the shell's loop.

## Choose the application and models

```sh
uv run hinoki shell --models notes.apps.billing.models
uv run hinoki shell notes.main:app
```

In a project the target defaults to `project.application`, and every registered
app's models are loaded; repeat `--models` to load only some. Outside a project,
pass a target (`module:app`); `--models` is then optional.

## What starts and what does not

Each module the application installs, the database first, starts in dependency
order and closes when the session ends. The application's `on_startup` and
`on_shutdown` hooks do not run, so nothing sends mail or seeds data behind your
back, and migrations are never applied: run
[`hinoki migrations apply`](migrations.md) yourself.

Unmanaged asyncio tasks are cancelled on exit; cleanup is bounded by the
`lifecycle.shutdown_timeout` setting (10 seconds by default), and a task that ignores cancellation is abandoned at
that deadline. Startup is bounded too: if a module suppresses cancellation while
opening, the shell stops the loop and cannot guarantee that the module released
what it had opened.

!!! warning "Real data, your privileges"
    Code at the prompt runs with your user's privileges against the configured
    database and can change it. Point the settings at a development database.

## How it looks

On a colour terminal the shell opens with the Hinoki banner, a summary (the
application title, the settings class and the loaded models) and a `hinoki ❯`
prompt. `NO_COLOR`, `TERM=dumb`, `HINOKI_ANIMATE=0` and `HINOKI_IMAGES=0`
behave as for [`hinoki new`](new-project.md#how-it-looks-in-a-terminal); without
colour the shell prints `Hinoki interactive shell; use Ctrl-D or Ctrl-C to exit.`
and Python's `>>>` / `...` prompts.

## Errors and edge cases

- Without a terminal the shell exits 2: "`hinoki shell` requires an interactive
  terminal".
- Ctrl-D exits, Ctrl-C at the prompt exits, Ctrl-C during a cell interrupts that
  cell.
- A model module that does not import stops the shell with
  `cannot load model module '…'`; a duplicate class name with
  `model name 'Invoice' occurs in both '…' and '…'`. Both exit 2.
- Outside a project without a target: `pass TARGET outside a project with
  hinoki.toml`.

## See also

- [Models](models.md)
- [Own resources with modules](modules.md)
- [Command line](cli.md)
