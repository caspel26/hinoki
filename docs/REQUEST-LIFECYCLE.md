# Request lifecycle

The order matters because validation, access and transactions have different failure behavior.

1. Granian accepts the HTTP request and calls the ASGI application.
2. Hinoki matches the method and path and binds path, query, header and body values.
3. Input schemas validate before the handler runs.
4. An action's authentication provider resolves a caller; its access policy checks permissions.
5. For `transaction=True`, Hinoki opens a transaction before calling the handler.
6. The handler returns. The declared response type is validated and rendered.
7. The transaction commits after successful response preparation. An exception or cancellation rolls back before an HTTP error is produced.

An unhandled exception does not become a successful response. A failed commit is an error. A response renderer should not perform hidden database I/O.

The default `execution.request_timeout` is 30 seconds and covers request processing through response preparation. Expiry returns `504 timeout`; an open transaction is rolled back. A synchronous handler already running in a worker thread cannot be forcibly stopped, so it continues occupying its executor slot until it returns, and its result is not sent. The default `execution.max_body_bytes` is 1 MiB; an oversized body returns `413 payload_too_large`.

Each request gets a generated `x-request-id` response header. Built-in error JSON includes the same value as `request_id`. Startup and shutdown have separate defaults of 30 and 10 seconds in `lifecycle.startup_timeout` and `lifecycle.shutdown_timeout`; startup failure is reported through the ASGI lifespan protocol, and shutdown runs resource cleanup under its deadline.

The router, parsing, validation and selected serialization work use Rust, but Granian and the ASGI adapter cross Python objects. See [sync and async](SYNC-ASYNC.md) for what runs on which executor.
