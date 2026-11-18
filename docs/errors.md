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

## Your own errors need no registration

Every `APIError` subclass answers by itself with its `status`, `code` and `message`. Pick the base class whose status you want and `raise` it:

| Base class | Status | Use it for |
| --- | --- | --- |
| `errors.BadRequest` | 400 | a malformed or refused request |
| `errors.Unauthorized` | 401 | the caller is not authenticated |
| `errors.Forbidden` | 403 | the caller may not do this |
| `errors.NotFound` | 404 | the resource does not exist |
| `errors.Conflict` | 409 | the request contradicts the current state (a duplicate, already done) |
| `errors.TooManyRequests` | 429 | a route's [rate limit policy](endpoints.md#rate-limit-an-action) refused the request; `retry_after` seconds are sent as `Retry-After` |
| `errors.APIError` | the `status` you set | anything else |

```python
class PaymentDeclined(errors.BadRequest):        # answers 400 on its own
    code = "payment_declined"
    message = "The payment was declined."

class QuotaExceeded(errors.APIError):            # any other status
    status = 429
    code = "quota_exceeded"
```

Declare them on the action with `errors=(PaymentDeclined,)` for the contract (OpenAPI, MCP, `strict_errors`). Nothing else is needed.

## Exceptions you cannot change

A library or the database raises its own exception, which is not an HTTP error, and the client would receive `500 internal_error`. Answer it with a function on the app, as FastAPI does. The function receives the exception and returns the `APIError` to send; an instance can set its own `status`:

```python
app = Hinoki(include=[...])

@app.error_handler(stripe.CardError)
def card_error(error: stripe.CardError) -> errors.APIError:
    return errors.APIError(error.user_message, status=402, code="card_declined")
```

* The most specific class wins: a handler for `HardQuota` is used before one for its base `Quota`. Application handlers are consulted before the built-in ones.
* A handler may be `def` or `async def`. One that fails, or does not return an `APIError`, counts as "not handled" and the client receives `500 internal_error` without the exception text.
* An exception type can be registered once, and only before the app serves (afterwards `RegistrationError`). The decorator returns the function unchanged. A class or a value that is not a function is refused with `TypeError`.
* `app.errors.handle(Kind)` is the same call without the `app` checks.
* With a factory (`create_app()`), call `app.error_handler(Kind)(handler)` after building the app; the decorator form needs the application at module level. See [one application object](concepts.md#one-application-object).

Built in, with nothing to write: `ObjectNotFound` is answered with `404 not_found`, and a database `UniqueViolation` with `409 unique_conflict` (`errors.UniqueConflict`) without exposing the constraint name. Register your own handler to replace either.

## Validation and binding errors

Input that does not match a path, query, header or body annotation fails before the handler runs. Schema validation is strict and reports field paths with `422`. Unsupported content types receive `415`; malformed supported content receives a parser error. A body larger than `execution.max_body_bytes`, or a multipart part larger than its parser's `max_part_bytes`, receives `413 payload_too_large`. Query strings over `execution.max_query_bytes` receive `414 uri_too_long`; combined headers over `execution.max_header_bytes` receive `431 headers_too_large`. A `Host` outside the `allowed_hosts` setting receives `400 disallowed_host`, and a CORS preflight from an origin outside `cors.origins` receives `403 cors_origin_denied` ([settings](settings.md#debug-allowed-hosts-and-cors)). These are framework-level failures and do not need to be listed in the action's `errors=`.

When `security.redirect_https` is enabled, a plain HTTP request receives `308` with an HTTPS `Location` before routing or body parsing. The redirect requires an explicit `allowed_hosts` list and preserves the request method and target. A target that is not a path, such as `OPTIONS *`, is not redirected. See [browser security settings](settings.md#browser-security-headers-and-https).

`ModelValidationError` is raised before an ORM insert or update when a Model field constraint or custom validator refuses a value. In an HTTP request it uses the same `422 validation_error` response shape as Schema validation, including field paths, codes and messages. The admin displays the field messages in its form. Keep Schema validation at the request boundary; Model checks also cover non-HTTP writes. See [Model field constraints and validators](models.md#model-field-constraints-and-validators).

Every HTTP request receives a request ID in the configured `server.request_id_header` header (`x-request-id` by default). Hinoki generates the ID unless `server.trust_request_id=True` and the request comes from a configured trusted proxy with a valid inbound ID. Built-in error bodies also include `request_id`. Use the ID to connect a client report with the server log entry. Unhandled exceptions are logged with the ID and returned as `500 internal_error` without exposing exception text. The `debug` setting adds the exception type, message and traceback to that answer for development.

## Check the contract

Use `ASGIClient(..., strict_errors=True)` in tests to fail if a handler returns an error that it did not declare. `hinoki check` can also enforce rules about access and transaction declarations; see [rules](rules.md).

Earlier revisions exported these names from `hinoki` itself and kept the HTTP errors in `hinoki.httperrors`; both are gone. Use `errors.<Name>`; `from hinoki import NotFound` raises `ImportError`.
