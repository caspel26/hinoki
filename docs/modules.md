---
status: implemented
---

# Application modules and resources

Modules own resources whose lifetime belongs to a worker or a management command, such as a database pool. They are created during startup, made available through `ModuleContext`, and closed when that run ends.

## Declare modules

```python
class AppSettings(FrameworkSettings, prefix="APP_"):
    database: DatabaseSettings


app = Hinoki(
    settings=AppSettings,
    include=[health, Tasks],
    modules=[DatabaseModule],
)
```

When a module class is passed, Hinoki constructs it from the settings class. A module instance is useful when setup needs explicit runtime arguments. The database pool is opened after the worker starts and closed during shutdown; importing the model module does not open a connection.

## Provide and retrieve dependencies

A module calls `ctx.provide(Type, value)` during lifespan startup. Other modules can declare dependencies and retrieve the provided value with `ctx.get(Type)`. Dependencies determine startup and shutdown order. Missing, duplicate or circular providers fail application startup.

Module lifespan hooks are async. Keep startup bounded, fail fast when a required service is unavailable, and close each owned resource in `finally`.

## Standalone scripts

For app maintenance tasks, declare a [management command](cli.md#manage) and list its required module classes in `Command.requires`. Hinoki starts only those modules and their dependencies for one command run, so a database module is opened and closed without starting the HTTP server or unrelated modules.

Standalone scripts outside Hinoki's server or management-command runner have no module lifespan. Open their own `Database` and close it explicitly; do not import a module-global pool. The blog seed script demonstrates this pattern.

See [settings and database ownership](settings.md#8-database-connection-ownership), [serving](serving.md) and [sync and async](sync-async.md).
