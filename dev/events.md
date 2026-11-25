# Events

An event is a class; a handler is a function registered for that class. Events let one part of an application announce that something happened without importing the parts that react. Nothing registers by import: the application lists its handler collections, and the order they run in is the order of that list.

Events are typed (mypy checks the payload), they have no string names, and the moment a handler runs is part of how you register it. Events are not a message queue: delivery stays inside one worker process, with no retry and no at-least-once guarantee.

## A minimal example

```python
from hinoki import Hinoki, Schema, events, http


class NoteSaved(Schema):
    text: str


class Saved(Schema):
    status: str


handlers = events.Handlers("notes")


@handlers.after_commit(NoteSaved)
async def tell_search(event: NoteSaved) -> None:
    print("index", event.text)


@http.post("/notes", access=None)
async def save() -> Saved:
    await events.emit(NoteSaved(text="hello"))
    return Saved(status="saved")


app = Hinoki(events=[handlers], include=[save])
```

`events=` and `include=` are passed by hand here because the example is one file. A project lists the collection in `hinoki.toml` instead ([below](https://hinoki.caspel.dev/dev/events.md#declaring-handlers-in-hinokitoml)). The [Tracker example](https://github.com/caspel26/hinoki/blob/main/examples/tracker/tracker/apps/activity/events.py) keeps its handlers in `tracker/apps/activity/events.py`, and the [Blog example](https://github.com/caspel26/hinoki/blob/main/examples/blog/blog/apps/blog/events.py) also emits its own event from an action.

Your own events can be a `Schema` (as above: frozen, keyword-only and validated when built) or a frozen `dataclass`; registration accepts any class. A `Schema` costs a validation per event, and it cannot hold a model class or instance, so the built-in model events below (`subject: type[M]`, `instance: M`) are dataclasses and an event that carries a row should carry its identifier instead.

`events.emit` uses the bus of the current worker. Outside a request, a startup hook or a management command it raises `EventError` unless you wrap the code in `events.use(app.events)`.

## Two moments, named in the registration

| Decorator | Runs | May fail the write | Function kind |
| --- | --- | --- | --- |
| `@handlers.in_transaction(E)` | Inside the writer's transaction, when the event is emitted | Yes: an exception rolls the transaction back | `async def` only |
| `@handlers.after_commit(E)` | After the outermost transaction commits | No | `def` or `async def` |

- An `in_transaction` handler must be quick and may only use the active transaction's connection. It is `async def` only because its timeout can be enforced on awaiting code only, and a synchronous call on another thread cannot join a transaction another task opened. Emitting such an event with no transaction active raises `EventError`.
- An `after_commit` handler is queued on the transaction and dropped if the transaction (or its savepoint) rolls back. Outside a transaction it runs at once. Its failure is logged, counted and never propagated: the write has already committed. A plain `def` runs on the bounded sync executor.
- A handler that writes to the database from `after_commit` must open its own transaction.

## Typing and subjects

A handler's first parameter is annotated with the event class, and the registration is checked against it when the collection is built: a missing or mismatched annotation raises `EventRegistrationError` at startup, not at the first emit. An event with a `subject` attribute (a model class, for example) can be registered as `Event[Note]` to receive only that subject, or bare `Event` to receive every subject (annotate it `Event[Any]`).

## Order and limits

Handlers run in the order of the `events=` collections, then in registration order inside each collection. The limits are the `events` settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `events.handler_timeout` | `5.0` | Seconds each handler may run; `EventTimeout` when exceeded. |
| `events.max_handlers` | `16` | Handlers per (event, phase); registering more raises `EventRegistrationError`. |
| `events.max_depth` | `4` | How deep handlers may emit events from handlers; more raises `EventError`, which stops cycles. |

An `in_transaction` failure or timeout propagates to the writer, which rolls back before any HTTP error is translated. An `after_commit` failure is isolated: the remaining handlers still run.

## Declaring handlers in `hinoki.toml`

```toml
[[apps]]
name = "activity"
models = "tracker.apps.activity.models"
migrations = "tracker.apps.activity.migrations"
routes = "tracker.apps.activity.api:routes"
events = "tracker.apps.activity.events:handlers"
requires = ["tasks", "members"]
```

```python
from hinoki import Hinoki

app = Hinoki()  # reads routes, events, middleware and rules from hinoki.toml
```

Events stay per app because a handler belongs to the models and behaviour of one app and runs in dependency order, whereas [middleware](https://hinoki.caspel.dev/dev/middleware.md#declaring-middleware-in-hinokitoml) and [rules](https://hinoki.caspel.dev/dev/rules.md#which-rules-run) are project-wide lists. When the manifest's `project.application` names the module that builds the app, `Hinoki()` loads `events=` if you did not pass it (independently of `include=`, `middleware=` and `rules=`); an explicit argument (even `()`) wins and skips that key. `manifest_events()` does the same load explicitly, for combining lists or building an app elsewhere. It imports each app's `Handlers` in dependency order (`requires`) and fails with a message naming the app when the target does not import or is not a `Handlers`. Hinoki does not generate the module: you create it yourself (for example `tracker/apps/activity/events.py` with `handlers = events.Handlers("activity")`, as in the Tracker example) and name it in the key, so there is no template to copy and a mistyped target is reported when the application is built. `hinoki app add notes --models ... --events pkg.events:handlers` registers an existing one; `hinoki app list` shows it.

## Testing

```python
with app.events.capture() as seen:
    ...  # code under test emits events
assert seen == [NoteSaved(text="hello")]

with app.events.muted():
    ...  # events emitted here run no handler
```

`capture()` records every event emitted in the block and still runs the handlers; `muted()` runs no handler (`capture()` still records what is emitted). `app.events.stats()` returns the counters of `(event, phase, outcome)`, which are low-cardinality by construction. `app.events.registrations` lists what is registered.

## Seeing what is registered

`hinoki events` lists every handler of the application without starting a server, opening a database or writing anything. It builds the application the way `hinoki routes` does and takes the same optional target (default: the project's `application`).

From `examples/tracker`:

```sh
uv run hinoki events
```

```text
Created[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_created
Updated[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_updated

limits: events.handler_timeout=5.0, events.max_handlers=16, events.max_depth=4
declared in hinoki.toml: activity -> tracker.apps.activity.events:handlers
```

Each row is the event (`Event` or `Event[Model]`), the phase, the name of the `Handlers` collection (`-` when it has none), `async` or `sync`, and the handler as `module.qualname`, in execution order. The last line shows the `events` limits in force. In a project, a line `declared in hinoki.toml: APP -> module:attribute` follows for each app that sets `events` in `hinoki.toml`. An application with no handlers prints `no event handlers registered` and exits `0`. `--format json` prints an object with `handlers` (`event`, `phase`, `handler`, `collection`, `async`), `limits` (`handler_timeout`, `max_handlers`, `max_depth`) and `apps` (`app`, `events`). A target that cannot be loaded prints `error: cannot load TARGET: ...` and exits `2`. The listing shows what is registered, not whether a handler will run: that depends on the code that emits.

## Authentication and rate-limit events

These events describe a request the application refused. They are plain frozen dataclasses (`Schema` cannot hold arbitrary types, and an event emitted on every refused request should cost nothing to build).

| Event | Payload | Emitted when |
| --- | --- | --- |
| `AuthRejected` | `reason`, `route`, `method` | A route's `Access` policy refused the request: `reason` is `"missing"` (no credentials, `401`), `"invalid"` (credentials the provider rejected, `401`) or `"forbidden"` (a caller without a required permission, `403`). Also for a WebSocket handshake (`method` is `WEBSOCKET`). |
| `RateLimited` | `policy`, `route`, `method`, `retry_after` | A route's `rate_limit` policy refused with `TooManyRequests` (`429`), including `RateLimit` and the admin's shared sign-in limit. A storage failure is not a refusal and emits no event. `policy` is the class name of the policy; `retry_after` is the whole seconds sent in `Retry-After`. |

```python
from hinoki import events
from hinoki.events import AuthRejected, RateLimited

security = events.Handlers("security")


@security.after_commit(AuthRejected)
async def count_rejection(event: AuthRejected) -> None:
    rejections.labels(event.reason, event.route).inc()


@security.after_commit(RateLimited)
def log_limit(event: RateLimited) -> None:
    log.warning(
        "rate limited %s %s (%ss)", event.method, event.route, event.retry_after
    )
```

- **No secrets, no high-cardinality values.** `route` is the path template (`/notes/{note_id}`), never the requested path or query string. The payloads carry no credential, token, user name, caller id, address or header, so a handler can count or log them without handling personal data. Add nothing of that kind if you define similar events.
- **They cannot change the answer.** The `401`, `403` or `429` is decided before the event is emitted. Only `after_commit` handlers are accepted: there is no transaction, so `@handlers.in_transaction(AuthRejected)` raises `EventRegistrationError` when it is registered (an event class opts out of transactions with the class attribute `has_transaction: ClassVar[bool] = False`). A failing handler is logged and counted and never reaches the response, and the whole delivery of one event, handlers included, takes at most `events.handler_timeout` seconds before the answer is sent. A slow handler still delays the refusal up to that limit, so keep handlers short and hand heavy work to a queue.
- **Zero cost without a listener.** The runtime builds the payload only when a handler is registered for the event (one context-variable read and one dictionary lookup).
- **Native lane.** Kiso authenticates generated CRUD and handler routes protected by an unmodified `ClaimsJWTAuth` in Rust and answers `401` and `403` there. When a handler is registered for `AuthRejected`, those routes are authenticated by Python so the event can fire (`hinoki routes --lanes` reports `auth_event_handlers` for generated CRUD). Without such a handler nothing changes. A route with an `access` policy or a `rate_limit` also stays off the native handler lane while a handler is registered for `AuthRejected` or `RateLimited`, because the event is delivered from an asyncio task; `hinoki routes` shows the reason.
- **What is not an event.** A `401`, `403` or `429` raised by your own handler code or by ownership checks inside generated CRUD is not an access-policy decision and emits nothing. There is no `AuthSucceeded`: it would fire on every authenticated request, and Rust authenticates the fast path, so it would turn that path off for everyone who listens.
- **Startup and shutdown.** Worker lifecycle, migration and configuration-reload events are not provided: use the `on_startup` and `on_shutdown` hooks of the application.

## Model events

`Created`, `Updated` and `Deleted` (`from hinoki.events import Created, Updated, Deleted`) are emitted by the model write path itself. Each carries `subject`, the model class, so `@handlers.after_commit(Created[Note])` receives only notes and a bare `Created` receives every model (annotate it `Created[Any]`).

| Event | Payload | Emitted by |
| --- | --- | --- |
| `Created[M]` | `subject`, `instance` (the stored instance) | `acreate`, `asave` of a new instance, `abulk_create` (one per row), generated CRUD create, admin create |
| `Updated[M]` | `subject`, `instance`, `changed` (the changed column names, read before the write) | `asave`, `aupdate_from`, `aupdate_within`, `abulk_update` (one per row that changed), generated CRUD update, admin edit. Nothing is emitted when no column changed. |
| `Deleted[M]` | `subject`, `count` (rows one DELETE statement removed) | `Model.adelete`, `QuerySet.adelete`, `abulk_delete` (one per chunk of keys), generated CRUD delete, admin delete. No key or row is loaded, so there is no instance; nothing is emitted when no row was deleted. |

```python
from hinoki import Hinoki, db, events
from hinoki.events import Created, Updated
from hinoki.orm import Model, pk


@db.table("notes")
class Note(Model):
    id: int = pk()
    title: str


@db.table("audit_lines")
class Audit(Model):
    id: int = pk()
    note_id: int


handlers = events.Handlers("notes")


@handlers.in_transaction(Created[Note])
async def audit(event: Created[Note]) -> None:
    # Runs in the insert's transaction: raising here rolls the note back.
    await Audit.acreate(id=event.instance.id, note_id=event.instance.id)


@handlers.after_commit(Updated[Note])
async def reindex(event: Updated[Note]) -> None:
    if "title" in event.changed:
        print("reindex", event.instance.id)


app = Hinoki(events=[handlers])
```

Any code the application runs (a request, a startup hook, a command) that calls `await Note.acreate(...)` or `await note.asave()` now emits; in a script or test wrap it in `with events.use(app.events):` and a database.

How it runs:

- A write emits only when the application's bus has a handler for that event and model. With none, the write takes the path it always took: one context-variable read and one dictionary lookup, with no extra await or allocation.
- With an `in_transaction` handler the write runs in a transaction (a savepoint inside an open one), so an exception or timeout in the handler rolls the row back and reaches the caller. With only `after_commit` handlers no transaction is added: the handler is queued on the open transaction and dropped on rollback, or runs at once when none is open. `Updated` and `Created` are emitted after the instance holds the stored row and after the [lifecycle hooks](https://hinoki.caspel.dev/dev/model-hooks.md), so the instance is what a hook left.
- A handler that writes another model with handlers emits again, bounded by `events.max_depth`.
- Not emitted: `QuerySet.aupdate`, `Model.aupdate`, `aupdate_where` (one UPDATE by filter, no instances), raw SQL and migrations. Use `asave` or `abulk_update` on loaded instances when a handler must see the change.
- Events never reveal rows a caller cannot see: they come from the same write that applied ownership and tenant scoping. Generated CRUD update saves the instance with the scoped UPDATE (a row that left the scope is refused before any event), and the `Updated` instance is the row the caller was allowed to write.
- Native lane: Kiso runs generated create, update and delete in Rust without the model, so a model with a handler for `Created`, `Updated` or `Deleted` (any phase, bare or for that model) loses native eligibility for the matching action only (`create`, `update`, `delete`); it runs on the Python lane, where the event fires. `hinoki routes` and the plan diagnostics report the reason `model_event_handlers`. A hand-written `async def` handler that awaits `acreate`, `abulk_create`, `abulk_update`, `adelete` or `abulk_delete` on such a model stays off the native handler lane for the same reason. Models without handlers are unchanged.

## Metrics

With [`metrics.listen`](https://hinoki.caspel.dev/dev/metrics.md) set on Kiso, every handler run is counted and timed on the metrics listener. The label set is fixed at startup from the registered handlers: `event` is the event class (`Created[Note]` for a handler registered for one model), `phase` is `in_transaction` or `after_commit`, and `outcome` is `ok`, `error` or `timeout`. No payload, handler name or request value becomes a label, so the series count never exceeds the number of registered (event, phase) pairs.

```text
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="ok",worker="0"} 3
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="error",worker="0"} 3
hinoki_events_total{event="NoteSaved",phase="after_commit",outcome="timeout",worker="0"} 3
hinoki_event_handler_duration_seconds_count{event="NoteSaved",phase="after_commit",worker="0"} 9
```

With metrics off, or under another server, `EventBus.stats()` still counts runs per worker and nothing else is recorded: a handler run pays one `None` check. See [export Prometheus metrics](https://hinoki.caspel.dev/dev/metrics.md#event-handlers).

## What events do not do

- To send an email from a handler, use `mail.send` in an `after_commit` handler: it runs once the data is durable. See [send email](https://hinoki.caspel.dev/dev/email.md).
- No delivery between services or processes, no queue, no retry, no at-least-once guarantee. For work that must survive a crash, use a job queue.
- Worker lifecycle, migration and reload events are not part of this release; use `on_startup` and `on_shutdown` for startup and shutdown.
- Events never carry rows a caller cannot see: emit them from code that already applied ownership and tenant policy, and put identifiers rather than whole rows in the payload.

See also [Lifecycle hooks](https://hinoki.caspel.dev/dev/model-hooks.md), [Transactions](https://hinoki.caspel.dev/dev/transactions.md) and [Application modules](https://hinoki.caspel.dev/dev/modules.md).
