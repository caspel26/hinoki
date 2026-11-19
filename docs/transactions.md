---
status: implemented
related:
  - endpoints.md
  - rules.md
---

# Transactions

An action opts into a database transaction with `atomic=True`. The transaction is opened for the request and commits only after the handler returns and the response body is encoded and rendered successfully. Hinoki checks the returned Python value against the handler's response annotation before it is encoded (see [the response check](endpoints.md#the-response-is-checked-against-the-annotation)); see also [OpenAPI limits](openapi.md#current-limits).

```python
@http.post("/tasks", atomic=True, status=201)
async def create_task(data: TaskInput) -> TaskOutput:
    task = await Task.acreate(data)
    await AuditEvent.acreate(task_id=task.id, action="created")
    return TaskOutput.from_model(task)
```

If the handler raises, response encoding/rendering fails, the request times out, or the task is cancelled, the transaction rolls back before the error response is translated. A value that does not match the response annotation is rejected as a `500`, and the transaction rolled back, unless the action opts out with `validate_response=False`. Do not send email or perform another irreversible side effect inside the transaction; arrange it after the transaction commits, or use an outbox owned by the application.

For a function route group, `RouteGroup(..., atomic=True)` makes write methods (`POST`, `PUT`, `PATCH`, `DELETE`) atomic by default. An individual action can opt out explicitly with `atomic=False`.

```python
tasks = RouteGroup("/tasks", access=member_only, atomic=True)


@tasks.post("/", status=201)  # atomic
async def create_task(data: TaskInput) -> TaskOutput: ...


@tasks.post("/{task_id}/ping", atomic=False)  # not atomic
async def ping(task_id: int) -> None: ...
```

## Atomic blocks and functions in application code

`db.atomic` (`from hinoki import db`) opens the same kind of scope in your own code, on the database that is current for the task: the one a request makes active, or the one set with `hinoki.orm.use_database(...)`. Use it as a block:

```python
from hinoki import db


async def transfer(source: Account, target: Account, amount: int) -> None:
    async with db.atomic():
        source.balance -= amount
        target.balance += amount
        await source.asave()
        await target.asave()
```

or as a decorator on an `async def`, with or without parentheses:

```python
@db.atomic
async def close_project(project_id: int) -> None:
    await Task.filter(project_id=project_id).aupdate({"done": True})
    await Project.aupdate(id=project_id, closed=True)


@db.atomic()
async def reopen_project(project_id: int) -> None: ...
```

Each call of a decorated function opens its own scope: returning commits, an exception rolls back and propagates. The wrapper keeps the function's name, docstring, signature and annotations (`functools.wraps`).

In a sync handler, hook or helper, use the same API without `await`: `with db.atomic():` as a block, or `@db.atomic` on a plain `def`. The sync model methods (`create`, `update`, `bulk_create`, ...) inside it run in the same transaction:

```python
@db.atomic
def close_project_sync(project_id: int) -> None:
    Task.update_where({"project_id": project_id}, {"done": True})
    Project.update(id=project_id, closed=True)

def archive(project: Project) -> None:
    with db.atomic():
        project.archived = True
        project.save()
```

The sync forms only work on a worker thread served by Hinoki, as the sync model methods do; on the event loop they raise `RuntimeError` (use `async with` there). A nested sync scope is a savepoint, like the async one. Raw `Database.execute` and `fetch` remain async-only (see [sync and async](sync-async.md)).

`db.atomic()` is `current_database().atomic()` resolved when the block is entered or the function is called. When you hold a `Database` yourself (a script, a module lifespan, `ctx.get(Database)`), call `database.atomic()` directly; it behaves the same. `Database.migration()` is the separate, exclusive scope used for migrations.

### Nesting and savepoints

A scope opened while another scope of the same task is already open becomes a savepoint, whether it comes from `atomic=True`, `async with`, or a decorated function. An exception inside the inner scope rolls back only that savepoint; the outer transaction continues and commits when it ends. A decorated function called from an `atomic=True` action therefore joins the request's transaction as a savepoint rather than committing on its own.

```python
async with db.atomic():
    await Order.acreate(data)
    try:
        async with db.atomic():  # SAVEPOINT
            await Coupon.aupdate(id=coupon_id, used=True)
            raise CouponExpired()
    except CouponExpired:
        pass  # only the coupon update is undone
# the order commits here
```

A child task cannot share its parent's active transaction: opening a nested scope from another task raises `DatabaseError`.

### Decorated functions and routes

A function decorated with `@db.atomic` can also be a route handler (put the HTTP decorator outermost). Binding, OpenAPI and the native server see the original signature, and the handler runs inside its own atomic scope. That scope covers the handler call only: it commits when the handler returns, before the response is encoded. `atomic=True` on the action is the declaration that also covers response encoding, and it is what `HNK-TXN-001` (`Transactions.require_atomic`) reads: the rule does not inspect decorators, so an action that is only decorated with `@db.atomic` is still reported. Prefer `atomic=True` on actions and keep `db.atomic` for application code that the action calls.

On the native server lane, a handler is still Python code: Rust calls the Python function, so a `@db.atomic` function behaves exactly as on the Python pipeline, including the rollback when it raises.

## After-commit callbacks

`Database.after_commit(callback)` (for example `current_database().after_commit(...)`) registers a sync or async callable without arguments to run after the outermost transaction commits, which is where email, queue publishing and cache invalidation belong. Callbacks run in registration order; a rollback of the transaction, or of the savepoint scope that registered them, drops them, and an exception in one is logged without undoing the commit. Calling it outside a transaction raises `DatabaseError`. Model hooks register through `self.after_commit(callback)`; see [lifecycle hooks](models.md#lifecycle-hooks-and-instance-state).

```python
from hinoki import db
from hinoki.orm import current_database

async with db.atomic():
    order = await Order.acreate(data)
    current_database().after_commit(lambda: publish("order.created", order.id))
```
