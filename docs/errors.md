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

## Errors that are not `APIError`

An exception from the database, a library or your own domain layer is not an HTTP error, so the client would receive `500 internal_error`. Register how to answer it on the app. The value is an `APIError` subclass, or a function of the exception when the answer depends on it:

```python
class CardDeclined(errors.Conflict):
    code = "card_declined"
    message = "The card was declined."

app = Hinoki(include=[...])

@app.error_handler(PaymentDeclined)           # a decorator: the class is returned unchanged
class _(CardDeclined): ...

app.error_handler(PaymentDeclined)(CardDeclined)       # the same, without a new class
app.register_errors({PaymentDeclined: CardDeclined})   # several at once

@app.error_handler(QuotaExceeded)             # a function when the answer depends on the exception
def quota(error: QuotaExceeded) -> errors.Conflict:
    return errors.Conflict(f"Limit is {error.limit}.", code="quota_exceeded")
```

| Case | What you write | Registration |
| --- | --- | --- |
| A business rule that is already an HTTP error | a subclass of `errors.Conflict`, `errors.NotFound`, ... and `raise` it | none: the runtime answers with its status and `code` |
| An exception from outside or from your domain layer | `app.error_handler(Kind)` or `app.register_errors({...})` | on the app |
| The answer depends on the exception | a function passed to `app.error_handler(Kind)` | on the app |

Rules that apply to every registration:

* The most specific class wins: a handler for `HardQuota` is used before one for its base `Quota`. Application handlers are consulted before the built-in ones.
* A handler may be `def` or `async def`. One that fails, or does not return an `APIError`, counts as "not handled" and the client receives `500 internal_error` without the exception text.
* An exception type can be registered once; `register_errors` registers all entries or none. Registering on an application that is already serving raises `RegistrationError`.
* `errors=(...)` on an action is only the declared contract (OpenAPI, MCP, `strict_errors`); it takes `APIError` subclasses, never plain exceptions.

Built in: `ObjectNotFound` is answered with `404 not_found`, and a database `UniqueViolation` with `409 unique_conflict` (`errors.UniqueConflict`) without exposing the constraint name. Register your own to replace either.

## Validation and binding errors

Input that does not match a path, query, header or body annotation fails before the handler runs. Schema validation is strict and reports field paths with `422`. Unsupported content types receive `415`; malformed supported content receives a parser error. A body larger than `execution.max_body_bytes`, or a multipart part larger than its parser's `max_part_bytes`, receives `413 payload_too_large`. Query strings over `execution.max_query_bytes` receive `414 uri_too_long`; combined headers over `execution.max_header_bytes` receive `431 headers_too_large`. A `Host` outside the `allowed_hosts` setting receives `400 disallowed_host`, and a CORS preflight from an origin outside `cors.origins` receives `403 cors_origin_denied` ([settings](settings.md#debug-allowed-hosts-and-cors)). These are framework-level failures and do not need to be listed in the action's `errors=`.

When `security.redirect_https` is enabled, a plain HTTP request receives `308` with an HTTPS `Location` before routing or body parsing. The redirect requires an explicit `allowed_hosts` list and preserves the request method and target. A target that is not a path, such as `OPTIONS *`, is not redirected. See [browser security settings](settings.md#browser-security-headers-and-https).

`ModelValidationError` is raised before an ORM insert or update when a Model field constraint or custom validator refuses a value. In an HTTP request it uses the same `422 validation_error` response shape as Schema validation, including field paths, codes and messages. The admin displays the field messages in its form. Keep Schema validation at the request boundary; Model checks also cover non-HTTP writes. See [Model field constraints and validators](models.md#model-field-constraints-and-validators).

Every HTTP request receives a request ID in the configured `server.request_id_header` header (`x-request-id` by default). Hinoki generates the ID unless `server.trust_request_id=True` and the request comes from a configured trusted proxy with a valid inbound ID. Built-in error bodies also include `request_id`. Use the ID to connect a client report with the server log entry. Unhandled exceptions are logged with the ID and returned as `500 internal_error` without exposing exception text. The `debug` setting adds the exception type, message and traceback to that answer for development.

## Check the contract

Use `ASGIClient(..., strict_errors=True)` in tests to fail if a handler returns an error that it did not declare. `hinoki check` can also enforce rules about access and transaction declarations; see [rules](rules.md).

Earlier revisions exported these names from `hinoki` itself and kept the HTTP errors in `hinoki.httperrors`; both are gone. Use `errors.<Name>`; `from hinoki import NotFound` raises `ImportError`.
