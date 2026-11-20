---
status: implemented
related:
  - concepts.md
  - transactions.md
  - sync-async.md
---

# Request lifecycle

The order matters: validation, access and transactions fail in different ways and at different moments.

1. **Accept.** Kiso accepts the connection, answers what needs no application code and hands the rest to the request pipeline ([Kiso](kiso.md)). With `--server granian`, Granian calls the ASGI application, which reaches the same pipeline.
2. **Route.** Hinoki matches the method and path.
3. **Authenticate and authorize.** For an action with an access policy, the provider resolves a `Caller` and the policy checks permissions. A `rate_limit=` policy runs next. The body has not been read yet, so an unauthenticated or rate-limited client cannot make the server buffer it.
4. **Bind.** Hinoki negotiates the response type from `Accept` (`406` when nothing fits), reads the body of `POST`, `PUT`, `PATCH` and `QUERY` requests, then binds and validates path, query, header and body values.
5. **Open the transaction** for an `atomic=True` action.
6. **Run the handler**, then check its value against the return annotation (a mismatch is `500 internal_error`, inside the transaction, unless the action sets `validate_response=False`) and render it. A returned `Response` is neither checked nor rendered.
7. **Commit** after the response is prepared. An exception or cancellation rolls back before the HTTP error is produced.

An unhandled exception never becomes a successful response, and a failed commit is an error. A renderer must not perform hidden database I/O.

## Limits

| Setting | Default | When exceeded |
| --- | --- | --- |
| `execution.request_timeout` | 30 s, through response preparation | `504 timeout`; an open transaction rolls back |
| `execution.max_body_bytes` | 1 MiB (also each multipart part) | `413 payload_too_large` |
| `execution.max_query_bytes` | 8 KiB | `414 uri_too_long` |
| `execution.max_header_bytes` | 32 KiB, names and values combined | `431 headers_too_large` |

The size checks run before routing. A sync handler already running when the timeout expires cannot be stopped: it keeps its executor slot until it returns, and its result is discarded. Let the proxy or ASGI server enforce its own request-line and header limits too.

## Request IDs

Each response carries a request ID in `server.request_id_header` (`x-request-id`), and built-in error JSON repeats it as `request_id`. Hinoki generates it; with `server.trust_request_id=True` it accepts a validated inbound ID, only from a trusted proxy.

## Startup and shutdown

`lifecycle.startup_timeout` (30 s) and `lifecycle.shutdown_timeout` (10 s) bound the worker lifespan. A startup failure is reported through the ASGI lifespan protocol; shutdown cleans up resources within its deadline. [Startup and shutdown hooks](modules.md#startup-and-shutdown-hooks) run inside those limits.

## Where Rust runs

Routing, parsing, validation and selected serialization run in Rust; the server hand-off (Granian and the ASGI adapter, or Kiso's queue) crosses Python objects. See [sync and async code](sync-async.md) for which executor runs what.

## See also

* [Transactions](transactions.md)
* [Errors](errors.md)
* [Serving](serving.md)
