---
status: implemented
---

# Errors

Hinoki separates malformed requests, authentication failures and application errors. Handlers may declare the application errors they can raise so clients and generated tools can inspect the contract.

## Built-in HTTP errors

Every public error type lives in one namespace: `from hinoki import errors`. It holds `errors.BadRequest`, `errors.Unauthorized`, `errors.Forbidden`, `errors.NotFound` and `errors.Conflict`, each derived from `errors.APIError`, plus the framework's own failures (`errors.RegistrationError`, `errors.ValidationError`, `errors.ObjectNotFound`, `errors.ErrorRegistry` and the others listed in the [reference](reference/web.md)). They carry an HTTP status and a stable `code`. Raise them when the response is an expected part of the API:

```python
from hinoki import errors, http


class TaskAlreadyDone(errors.Conflict):
    code = "task_already_done"
    message = "The task is already done."


@http.post("/tasks/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete(task_id: UUID) -> TaskOutput:
    ...
```

An action may declare several errors. The runtime maps a declared `APIError` to its status and response body. Unexpected exceptions remain server errors; do not expose exception text to clients.

## Validation and binding errors

Input that does not match a path, query, header or body annotation fails before the handler runs. Schema validation is strict and reports field paths with `422`. Unsupported content types receive `415`; malformed supported content receives a parser error. A body larger than `execution.max_body_bytes` receives `413 payload_too_large`. Query strings over `execution.max_query_bytes` receive `414 uri_too_long`; combined headers over `execution.max_header_bytes` receive `431 headers_too_large`. A `Host` outside the `allowed_hosts` setting receives `400 disallowed_host`, and a CORS preflight from an origin outside `cors.origins` receives `403 cors_origin_denied` ([settings](settings.md#debug-allowed-hosts-and-cors)). These are framework-level failures and do not need to be listed in the action's `errors=`.

Every HTTP request receives a generated request ID. The response includes it in the `x-request-id` header; built-in error bodies also include `request_id`. Use the ID to connect a client report with the server log entry. Unhandled exceptions are logged with the ID and returned as `500 internal_error` without exposing exception text. The `debug` setting adds the exception type, message and traceback to that answer for development.

## Check the contract

Use `ASGIClient(..., strict_errors=True)` in tests to fail if a handler returns an error that it did not declare. `hinoki check` can also enforce rules about access and transaction declarations; see [rules](rules.md).

Earlier revisions exported these names from `hinoki` itself and kept the HTTP errors in `hinoki.httperrors`; both are gone. Use `errors.<Name>`; `from hinoki import NotFound` raises `ImportError`.
