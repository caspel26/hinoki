# Request lifecycle

The order matters: validation, access and transactions fail in different ways and at different moments.

1. **Accept.** Kiso accepts the connection, answers what needs no application code and hands the rest to the request pipeline ([Kiso](https://hinoki.caspel.dev/dev/kiso.md)). With `--server granian`, Granian calls the ASGI application, which reaches the same pipeline.
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
| `execution.max_streams` | 64 open streams per worker | `503` with `Retry-After: 1`, before the handler runs |

The size checks run before routing. A sync handler already running when the timeout expires cannot be stopped: it keeps its executor slot until it returns, and its result is discarded. Let the proxy or ASGI server enforce its own request-line and header limits too.

## Streamed responses

For an action that returns `Stream`, `EventStream` or `File`, everything up to the moment the handler returns the object (access, rate limit, binding, validation, the handler, middleware and `request_timeout`) works as for any action, and an error there is the usual JSON answer. Before the handler runs, the worker takes one of its `execution.max_streams` places, or answers `503` with `Retry-After: 1`. The head is then sent, and the body is produced and sent piece by piece: the pieces are not checked against an annotation, a failure can only cut the connection, and the place is given back when the body ends. See [stream a response](https://hinoki.caspel.dev/dev/streaming.md).

## Request IDs

Each response carries a request ID in `server.request_id_header` (`x-request-id`), and built-in error JSON repeats it as `request_id`. Hinoki generates it; with `server.trust_request_id=True` it accepts a validated inbound ID, only from a trusted proxy.

## Startup and shutdown

`lifecycle.startup_timeout` (30 s) and `lifecycle.shutdown_timeout` (10 s) bound the worker lifespan. A startup failure is reported through the ASGI lifespan protocol; shutdown cleans up resources within its deadline. [Startup and shutdown hooks](https://hinoki.caspel.dev/dev/modules.md#startup-and-shutdown-hooks) run inside those limits.

## Where Rust runs

Routing, parsing, validation and selected serialization run in Rust; the server hand-off (Granian and the ASGI adapter, or Kiso's queue) crosses Python objects. See [sync and async code](https://hinoki.caspel.dev/dev/sync-async.md) for which executor runs what.

## See also

* [Transactions](https://hinoki.caspel.dev/dev/transactions.md)
* [Errors](https://hinoki.caspel.dev/dev/errors.md)
* [Serving](https://hinoki.caspel.dev/dev/serving.md)
