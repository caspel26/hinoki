# Events

An event is a class; a handler is a function registered for that class. Events let one part of an application announce that something happened without importing the parts that react. Nothing registers by import: the application lists its handler collections, and the order they run in is the order of that list.

Events are not Django signals: they are typed (mypy checks the payload), they have no string names, and the moment a handler runs is part of how you register it. They are not a message queue either: delivery stays inside one worker process, with no retry and no at-least-once guarantee.

## A minimal example

```python
from dataclasses import dataclass

from hinoki import Hinoki, events, http


@dataclass(frozen=True)
class NoteSaved:
    text: str


handlers = events.Handlers("notes")


@handlers.after_commit(NoteSaved)
async def tell_search(event: NoteSaved) -> None:
    print("index", event.text)


@http.post("/notes")
async def save() -> dict[str, str]:
    await events.emit(NoteSaved("hello"))
    return {"status": "saved"}


app = Hinoki(events=[handlers], include=[save])
```

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
assert seen == [NoteSaved("hello")]

with app.events.muted():
    ...  # events emitted here run no handler
```

`capture()` records every event emitted in the block and still runs the handlers; `muted()` records nothing and runs none. `app.events.stats()` returns the counters of `(event, phase, outcome)`, which are low-cardinality by construction. `app.events.registrations` lists what is registered.

## What events do not do

- No delivery between services or processes, no queue, no retry, no at-least-once guarantee. For work that must survive a crash, use a job queue.
- Model, authentication, rate-limit and lifecycle events, the `hinoki events` command, Prometheus metrics and the native-lane behaviour are not part of this first slice. Until the model events exist, application code emits its own events, and the existing [lifecycle hooks](model-hooks.md) and `db.after_commit` keep working unchanged.
- Events never carry rows a caller cannot see: emit them from code that already applied ownership and tenant policy, and put identifiers rather than whole rows in the payload.

See also [Lifecycle hooks](model-hooks.md), [Transactions](transactions.md) and [Application modules](modules.md).
