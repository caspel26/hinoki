# Request lifecycle

The order matters because validation, access and transactions have different failure behavior.

1. Granian accepts the HTTP request and calls the ASGI application. (With `--server rust` the native server accepts it, answers what needs no application code and hands the rest to the same request pipeline; see [serving](serving.md#the-rust-server).)
2. Hinoki matches the method and path and binds path, query, header and body values.
3. Input schemas validate before the handler runs.
4. An action's authentication provider resolves a caller; its access policy checks permissions.
5. For `transaction=True`, Hinoki opens a transaction before calling the handler.
6. The handler returns. Hinoki prepares the response: a configured renderer turns the value into bytes. The declared response type is not checked at runtime.
7. The transaction commits after successful response preparation. An exception or cancellation rolls back before an HTTP error is produced.

An unhandled exception does not become a successful response. A failed commit is an error. A response renderer should not perform hidden database I/O.

The default `execution.request_timeout` is 30 seconds and covers request processing through response preparation. Expiry returns `504 timeout`; an open transaction is rolled back. A synchronous handler already running in a worker thread cannot be forcibly stopped, so it continues occupying its executor slot until it returns, and its result is not sent. The default `execution.max_body_bytes` is 1 MiB; an oversized body returns `413 payload_too_large`. Raw query strings default to an 8 KiB limit (`execution.max_query_bytes`, `414 uri_too_long`), and combined header names/values default to 32 KiB (`execution.max_header_bytes`, `431 headers_too_large`). These checks run before routing and header parsing. The ASGI server or reverse proxy should also enforce its own request-line and header limits.

Each request gets a request ID in the configured `server.request_id_header` response header (`x-request-id` by default). Hinoki generates it by default; with `server.trust_request_id=True`, it accepts a validated inbound ID only from a configured trusted proxy. Built-in error JSON includes the same value as `request_id`. Startup and shutdown have separate defaults of 30 and 10 seconds in `lifecycle.startup_timeout` and `lifecycle.shutdown_timeout`; startup failure is reported through the ASGI lifespan protocol, and shutdown runs resource cleanup under its deadline. Application [startup and shutdown hooks](modules.md#startup-and-shutdown-hooks) run inside those limits.

The router, parsing, validation and selected serialization work use Rust, but the server hand-off (Granian and the ASGI adapter, or the Rust server's queue) crosses Python objects. See [sync and async](sync-async.md) for what runs on which executor.
