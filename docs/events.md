# Events

An event is a class; a handler is a function registered for that class. Events let one part of an application announce that something happened without importing the parts that react. Nothing registers by import: the application lists its handler collections, and the order they run in is the order of that list.

Events are not Django signals: they are typed (mypy checks the payload), they have no string names, and the moment a handler runs is part of how you register it. They are not a message queue either: delivery stays inside one worker process, with no retry and no at-least-once guarantee.

## A minimal example

```python
from hinoki import Hinoki, Schema, events, http


class NoteSaved(Schema):
    text: str


handlers = events.Handlers("notes")


@handlers.after_commit(NoteSaved)
async def tell_search(event: NoteSaved) -> None:
    print("index", event.text)


@http.post("/notes")
async def save() -> dict[str, str]:
    await events.emit(NoteSaved(text="hello"))
    return {"status": "saved"}


app = Hinoki(events=[handlers], include=[save])
```

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
name = "notes"
models = "shop.apps.notes.models"
migrations = "shop.apps.notes.migrations"
events = "shop.apps.notes.events:handlers"
```

```python
from hinoki import Hinoki
from hinoki.project import manifest_events, manifest_routes

app = Hinoki(include=manifest_routes(), events=manifest_events())
```

`manifest_events()` imports each app's `Handlers` in dependency order (`requires`) and fails with a message naming the app when the target does not import or is not a `Handlers`. `hinoki app create notes --events` writes `events.py` and the key; `hinoki app add notes --models ... --events pkg.events:handlers` registers an existing one; `hinoki app list` shows it.

## Testing

```python
with app.events.capture() as seen:
    ...  # code under test emits events
assert seen == [NoteSaved(text="hello")]

with app.events.muted():
    ...  # events emitted here run no handler
```

`capture()` records every event emitted in the block and still runs the handlers; `muted()` runs no handler (`capture()` still records what is emitted). `app.events.stats()` returns the counters of `(event, phase, outcome)`, which are low-cardinality by construction. `app.events.registrations` lists what is registered.

## Model events

`Created`, `Updated` and `Deleted` (`from hinoki.events import Created, Updated, Deleted`) are emitted by the model write path itself. Each carries `subject`, the model class, so `@handlers.after_commit(Created[Note])` receives only notes and a bare `Created` receives every model (annotate it `Created[Any]`).

| Event | Payload | Emitted by |
| --- | --- | --- |
| `Created[M]` | `subject`, `instance` (the stored instance) | `acreate`, `asave` of a new instance, `abulk_create` (one per row), generated CRUD create, admin create |
| `Updated[M]` | `subject`, `instance`, `changed` (the changed column names, read before the write) | `asave`, `aupdate_from`, `aupdate_within`, `abulk_update` (one per row that changed), generated CRUD update, admin edit. Nothing is emitted when no column changed. |
| `Deleted[M]` | `subject`, `count` (rows one DELETE statement removed) | `Model.adelete`, `QuerySet.adelete`, `abulk_delete` (one per chunk of keys), generated CRUD delete, admin delete. No key or row is loaded, so there is no instance; nothing is emitted when no row was deleted. |

```python
from hinoki import Hinoki, events
from hinoki.events import Created, Updated
from hinoki.orm import Model, pk


class Note(Model, table="notes"):
    id: int = pk()
    title: str


class Audit(Model, table="audit_lines"):
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
- With an `in_transaction` handler the write runs in a transaction (a savepoint inside an open one), so an exception or timeout in the handler rolls the row back and reaches the caller. With only `after_commit` handlers no transaction is added: the handler is queued on the open transaction and dropped on rollback, or runs at once when none is open. `Updated` and `Created` are emitted after the instance holds the stored row and after the [lifecycle hooks](model-hooks.md), so the instance is what a hook left.
- A handler that writes another model with handlers emits again, bounded by `events.max_depth`.
- Not emitted: `QuerySet.aupdate`, `Model.aupdate`, `aupdate_where` (one UPDATE by filter, no instances), raw SQL and migrations. Use `asave` or `abulk_update` on loaded instances when a handler must see the change.
- Events never reveal rows a caller cannot see: they come from the same write that applied ownership and tenant scoping. Generated CRUD update saves the instance with the scoped UPDATE (a row that left the scope is refused before any event), and the `Updated` instance is the row the caller was allowed to write.
- Native lane: Kiso runs generated create, update and delete in Rust without the model, so a model with a handler for `Created`, `Updated` or `Deleted` (any phase, bare or for that model) loses native eligibility for the matching action only (`create`, `update`, `delete`); it runs on the Python lane, where the event fires. `hinoki routes` and the plan diagnostics report the reason `model_event_handlers`. A hand-written `async def` handler that awaits `acreate`, `abulk_create`, `abulk_update`, `adelete` or `abulk_delete` on such a model stays off the native handler lane for the same reason. Models without handlers are unchanged.

## What events do not do

- No delivery between services or processes, no queue, no retry, no at-least-once guarantee. For work that must survive a crash, use a job queue.
- Authentication, rate-limit and lifecycle events, the `hinoki events` command and Prometheus metrics are not part of this release.
- Events never carry rows a caller cannot see: emit them from code that already applied ownership and tenant policy, and put identifiers rather than whole rows in the payload.

See also [Lifecycle hooks](model-hooks.md), [Transactions](transactions.md) and [Application modules](modules.md).
