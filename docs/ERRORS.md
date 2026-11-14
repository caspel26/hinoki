---
status: implemented
---

# Errors

Hinoki separates malformed requests, authentication failures and application errors. Handlers may declare the application errors they can raise so clients and generated tools can inspect the contract.

## Built-in HTTP errors

The public package exports `BadRequest`, `Unauthorized`, `Forbidden`, `NotFound` and `Conflict`, each derived from `APIError`. They carry an HTTP status and a stable `code`. Raise them when the response is an expected part of the API:

```python
class TaskAlreadyDone(Conflict):
    code = "task_already_done"
    message = "The task is already done."


@post("/tasks/{task_id}/complete", errors=(NotFound, TaskAlreadyDone))
async def complete(task_id: UUID) -> TaskOutput:
    ...
```

An action may declare several errors. The runtime maps a declared `APIError` to its status and response body. Unexpected exceptions remain server errors; do not expose exception text to clients.

## Validation and binding errors

Input that does not match a path, query, header or body annotation fails before the handler runs. Schema validation is strict and reports field paths. Unsupported content types receive `415`; malformed supported content receives a parser error. These are framework-level failures and do not need to be listed in the action's `errors=`.

## Check the contract

Use `ASGIClient(..., strict_errors=True)` in tests to fail if a handler returns an error that it did not declare. `hinoki check` can also enforce rules about access and transaction declarations; see [rules](RULES.md).
