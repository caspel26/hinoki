---
status: implemented
related:
  - endpoints.md
  - rules.md
---

# Transactions

An action opts into a database transaction with `transaction=True`. The transaction is opened for the request and commits only after the handler returns and the response body is encoded and rendered successfully. Hinoki checks the returned Python value against the handler's response annotation before it is encoded (see [the response check](endpoints.md#the-response-is-checked-against-the-annotation)); see also [OpenAPI limits](openapi.md#current-limits).

```python
@http.post("/tasks", transaction=True, status=201)
async def create_task(data: TaskInput) -> TaskOutput:
    task = await Task.acreate(data)
    await AuditEvent.acreate(task_id=task.id, action="created")
    return TaskOutput.from_model(task)
```

If the handler raises, response encoding/rendering fails, the request times out, or the task is cancelled, the transaction rolls back before the error response is translated. A value that does not match the response annotation is rejected as a `500`, and the transaction rolled back, unless the action opts out with `validate_response=False`. Do not send email or perform another irreversible side effect inside the transaction; arrange it after the transaction commits, or use an outbox owned by the application.

Nested `db.transaction()` scopes in the same owning task use savepoints. A child task cannot share its parent's active transaction. Raw database operations are async-only; see [sync and async](sync-async.md).

For a function route group, `RouteGroup(..., atomic=True)` makes write methods transactional by default. An individual action can opt out explicitly with `transaction=False`.
