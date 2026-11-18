---
status: implemented
related:
  - endpoints.md
  - rules.md
---

# Transactions

An action opts into a database transaction with `transaction=True`. The transaction is opened for the request and commits only after the handler returns and the response body is encoded and rendered successfully. Hinoki does not validate the returned Python value against the handler's response annotation at runtime; see [OpenAPI limits](openapi.md#current-limits).

```python
@http.post("/tasks", transaction=True, status=201)
async def create_task(data: TaskInput) -> TaskOutput:
    task = await Task.acreate(data)
    await AuditEvent.acreate(task_id=task.id, action="created")
    return TaskOutput.from_model(task)
```

If the handler raises, response encoding/rendering fails, the request times out, or the task is cancelled, the transaction rolls back before the error response is translated. A value that does not match the response annotation is not necessarily rejected. Do not send email or perform another irreversible side effect inside the transaction; arrange it after the transaction commits, or use an outbox owned by the application.

Nested `db.transaction()` scopes in the same owning task use savepoints. A child task cannot share its parent's active transaction. Raw database operations are async-only; see [sync and async](sync-async.md).

For a function route group, `RouteGroup(..., atomic=True)` makes write methods transactional by default. An individual action can opt out explicitly with `transaction=False`.

## After-commit callbacks

`db.after_commit(callback)` registers a sync or async callable without arguments to run after the outermost transaction commits, which is where email, queue publishing and cache invalidation belong. Callbacks run in registration order; a rollback of the transaction, or of the savepoint scope that registered them, drops them, and an exception in one is logged without undoing the commit. Calling it outside a transaction raises `DatabaseError`. Model hooks register through `self.after_commit(callback)`; see [lifecycle hooks](models.md#lifecycle-hooks-and-instance-state).

```python
async with db.transaction():
    order = await Order.acreate(data)
    db.after_commit(lambda: publish("order.created", order.id))
```
