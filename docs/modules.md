---
status: implemented
related:
  - settings.md
  - transactions.md
  - cli.md
---

# Own resources with modules

A module owns something that lives as long as a worker or a command run: a
connection pool, a client, a thread pool. It opens the resource at startup,
provides it to the rest of the application and closes it when the run ends.
Nothing is opened at import time.

```python
# notes/passwords.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hinoki import Module, ModuleContext
from hinoki.crypto import PasswordHasher


class Passwords(Module):
    name = "passwords"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        hasher = PasswordHasher()
        ctx.provide(PasswordHasher, hasher)
        try:
            yield
        finally:
            hasher.close()
```

Install it on the application. The rules come from `hinoki.toml`, so the call
names only what is specific to this app:

```python
# notes/main.py
from hinoki import Hinoki

from notes.passwords import Passwords
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    title="Notes",
    modules=[Passwords()],
)
```

Each worker runs `lifespan` up to `yield` before it answers requests, and the
`finally` block when it stops, even if a later module fails to start.

## Use what a module provides

`ctx.provide(Type, value)` registers a value under its type; `ctx.get(Type)`
returns it (or raises `LookupError`) and `ctx.find(Type)` returns `None` instead.
The value is reachable from:

| Where | How |
| --- | --- |
| Another module's `lifespan` | `ctx.get(PasswordHasher)`, with `requires = (Passwords,)` |
| A startup or shutdown hook | `ctx.get(PasswordHasher)` |
| A management command | `context.get(PasswordHasher)`, with `requires = (Passwords,)` |
| A rate-limit policy | `context.resource(PasswordHasher)` |

```python
# notes/commands.py
from argparse import ArgumentParser, Namespace

from hinoki.crypto import PasswordHasher
from hinoki.management import Command, CommandContext

from notes.passwords import Passwords


class HashPassword(Command):
    name = "hash-password"
    help = "print the Argon2id hash of a password"
    requires = (Passwords,)

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("password")

    async def handle(self, context: CommandContext, args: Namespace) -> None:
        print(await context.get(PasswordHasher).hash(args.password))
```

With `commands=[HashPassword]` on the application, `hinoki manage hash-password s3cret`
starts only `Passwords` (and what it requires), runs the command and closes it.
Request handlers can receive a module-provided value as a parameter when its
type inherits `Resource`. Model methods also find the worker's `Database`
through the ORM context. Ordinary module values remain available through
`ModuleContext` in lifespans, hooks and commands, and through the policy context
where supported.

## Inject a worker resource into a handler

Declare a concrete `Resource` type, provide its instance from a module, then use
that type as a handler parameter. Hinoki resolves the annotation when routes are
registered and injects the worker's instance for each request:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from hinoki import Hinoki, Module, ModuleContext, Resource, http


class Mailer(Resource):
    async def send(self, address: str, subject: str) -> None: ...


class MailerModule(Module):
    name = "mailer"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        mailer = Mailer()
        ctx.provide(Mailer, mailer)
        yield


@http.post("/welcome")
async def welcome(email: str, mailer: Mailer) -> dict[str, str]:
    await mailer.send(email, "Welcome")
    return {"status": "sent"}


app = Hinoki(include=[welcome], modules=[MailerModule()])
```

The same annotation works on function handlers, synchronous `def` handlers and
methods on an `API` class. A resource parameter does not cause Hinoki to build a
`Request`; declare `request: Request` only when the handler needs request data.
Resource parameters are not client inputs, so they do not appear in the OpenAPI
document or MCP tool schema. Kiso also leaves them out of the Rust route input
list; a route can keep its native route specification when its other inputs and
handler behavior meet the native-path requirements.

Resources are mandatory worker contracts. A default value, a name that collides
with a `{path}` parameter, or two parameters requesting the same resource type
raises `RegistrationError` during registration. After all modules start and
before startup hooks run, Hinoki checks every required type with
`ModuleContext.get`. A missing provider fails startup with the route and type in
the error; the worker does not become ready.

## The database module

A `database: settings.DatabaseSettings` field on your settings class is enough:
Hinoki installs `DatabaseModule` (from `hinoki.db`) before any declared module, so
a module with `requires = (DatabaseModule,)` can call `ctx.get(Database)` in its
lifespan. Declaring `DatabaseModule`, or a subclass of it, in `modules=` replaces
the automatic one; modules that require `DatabaseModule` still start after it.
See [database connection ownership](settings.md#8-database-connection-ownership).

## The email module

An `email: settings.EmailSettings` field installs `EmailModule` (from `hinoki.mail`) the same way: one `Mailer` per worker, opened in the lifespan and closed at shutdown, available as `ctx.get(Mailer)`, to `mail.send(...)` in handlers and hooks, and to commands that declare `requires = (EmailModule,)`. Declaring `EmailModule` in `modules=` replaces the automatic one. See [send email](email.md).

## Declare modules

`modules=` accepts instances and classes. An instance is the plain choice; a class
is built with its `from_settings(settings)` classmethod, which you override to
read your settings (the default raises `TypeError` and says so).

`requires` decides the order: dependencies start first and stop last. An empty or
duplicate `name`, a missing requirement, a cycle, or a type provided twice fails
startup with `ModuleGraphError`. A module whose class extends a required class
satisfies that requirement.

Keep `lifespan` bounded (it shares `lifecycle.startup_timeout`), fail fast when a
required service is unavailable, and close what you own in `finally`.

## Startup and shutdown hooks

For an action at the edge of a worker's life that owns no resource, such as
warming a cache, logging the build or flushing a counter, list functions on the
application instead of writing a module (`ProjectSettings` here declares a
`database` field, so the database module is installed):

```python
# notes/main.py
import logging

from hinoki import Hinoki, ModuleContext
from hinoki.db import Database

from notes.settings import ProjectSettings

log = logging.getLogger("notes")


async def warm_cache(ctx: ModuleContext) -> None:
    await ctx.get(Database).fetch("SELECT 1")


def say_goodbye(ctx: ModuleContext) -> None:
    log.info("worker stopping")


app = Hinoki(
    settings=ProjectSettings, on_startup=[warm_cache], on_shutdown=[say_goodbye]
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

## Scripts outside the server

For app maintenance tasks, declare a [management command](cli.md#manage) and list its required module classes in `Command.requires`. Hinoki starts only those modules and their dependencies for one command run, so a database module is opened and closed without starting the HTTP server or unrelated modules.

Standalone scripts outside Hinoki's server or management-command runner have no module lifespan. Open their own `Database` and close it explicitly; do not import a module-global pool. The blog seed script demonstrates this pattern.

## Errors and edge cases

- `ctx.get` for a type nobody provided raises `LookupError: <Type> was not provided`; declare the providing module in `requires`.
- A module class without `from_settings` in `modules=` fails at construction: pass an instance instead.
- `hinoki check`, `routes` and `migrations` do not start modules or run hooks.

## See also

- [Settings](settings.md): where a module reads its configuration.
- [Command line](cli.md#manage): running management commands.
- [Sync and async](sync-async.md): where sync hooks run.
