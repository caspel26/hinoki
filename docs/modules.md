---
status: implemented
---

# Application modules and resources

Modules own resources whose lifetime belongs to a worker or a management command, such as a database pool. They are created during startup, made available through `ModuleContext`, and closed when that run ends.

## Declare modules

This excerpt uses the `health` route and `Tasks` API from your application; the imports and application-specific declarations are omitted here.

```python
class ProjectSettings(settings.AppSettings, prefix="APP_"):
    database: settings.DatabaseSettings


app = Hinoki(settings=ProjectSettings, include=[health, Tasks])
```

The settings' `database: DatabaseSettings` field makes Hinoki install `DatabaseModule` before any declared module, so a module that `requires = (DatabaseModule,)` finds it. Declaring `DatabaseModule` in `modules=`, or installing an instance, replaces that automatic module. When a module class is passed, Hinoki constructs it from the settings class. A module instance is useful when setup needs explicit runtime arguments. The database pool is opened after the worker starts and closed during shutdown; importing the model module does not open a connection.

## Provide and retrieve dependencies

A module calls `ctx.provide(Type, value)` during lifespan startup. Other modules can declare dependencies and retrieve the provided value with `ctx.get(Type)`. Dependencies determine startup and shutdown order. Missing, duplicate or circular providers fail application startup.

A module instance whose class extends a required module class satisfies that dependency. For example, a custom `DatabaseModule` subclass can replace the settings-provided database while modules requiring `DatabaseModule` continue to start after it.

Module lifespan hooks are async. Keep startup bounded, fail fast when a required service is unavailable, and close each owned resource in `finally`.

## Startup and shutdown hooks

For an action at the edge of a worker's life that owns no resource, such as warming a cache, logging the build or flushing a counter, list functions on the application instead of writing a module (`ProjectSettings`, `health` and `Tasks` are those of the previous example):

```python
import logging

from hinoki import Hinoki, ModuleContext
from hinoki.db import Database

log = logging.getLogger("app")


async def warm_cache(ctx: ModuleContext) -> None:
    await ctx.get(Database).fetch("SELECT 1")


def say_goodbye(ctx: ModuleContext) -> None:
    log.info("worker stopping")


app = Hinoki(
    settings=ProjectSettings,
    include=[health, Tasks],
    on_startup=[warm_cache],
    on_shutdown=[say_goodbye],
)
```

A hook takes the worker's `ModuleContext` (`ctx.settings`, `ctx.get(Type)`, `ctx.tasks`). It is `async def`, or a plain `def` that runs on the bounded sync executor like a sync handler. Model methods reach the worker's database inside a hook as they do inside a request.

Pass hooks as a `list` or `tuple` when constructing `Hinoki`; the app's `on_startup` and `on_shutdown` tuples are read-only. Hooks must be callables, not async generators, and must return `None`. A plain `def` may instead return an awaitable, which Hinoki awaits; use `async def` when the hook needs to await work directly.

| | Startup hooks | Shutdown hooks |
| --- | --- | --- |
| Run | Once per worker, after every module has started, in the listed order | Once per worker, before background tasks, the sync executor and the modules are closed, in the listed order |
| Time limit | Share `lifecycle.startup_timeout` with the modules | Each has its own `lifecycle.shutdown_timeout` |
| Requests | Answered `503 not_ready` until the last hook returns | Answered `503 not_ready` from the moment shutdown begins |
| A hook fails or times out | Startup fails: later hooks do not run, no shutdown hook runs, the modules already started are closed, and the server is told that startup failed. The log has the traceback; the message names the hook (`startup hook 'warm_cache' failed to start: TimeoutError`) | It is logged and reported as a shutdown error; the remaining hooks and the module cleanup still run |

Shutdown hooks run only after a startup that completed, and a shutdown hook is not the `finally` of a startup hook. Use this rule to choose: **a resource you own is a module** (its lifespan closes it even when a later step of startup fails); **an action is a hook**. A plain `def` hook that is still running when its time limit passes cannot be interrupted; it keeps its executor thread until it returns.

Hooks run where the application serves requests: under `hinoki dev`/`serve` and any ASGI server, on the first request of each WSGI worker, and in the [test clients](testing.md). `hinoki manage` commands, `check`, `routes` and `migrations` do not run them.

## Standalone scripts

For app maintenance tasks, declare a [management command](cli.md#manage) and list its required module classes in `Command.requires`. Hinoki starts only those modules and their dependencies for one command run, so a database module is opened and closed without starting the HTTP server or unrelated modules.

Standalone scripts outside Hinoki's server or management-command runner have no module lifespan. Open their own `Database` and close it explicitly; do not import a module-global pool. The blog seed script demonstrates this pattern.

See [settings and database ownership](settings.md#8-database-connection-ownership), [serving](serving.md) and [sync and async](sync-async.md).
