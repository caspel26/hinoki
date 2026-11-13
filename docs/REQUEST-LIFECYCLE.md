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

The router, parsing, validation and selected serialization work use Rust, but Granian and the ASGI adapter cross Python objects. See [sync and async](SYNC-ASYNC.md) for what runs on which executor.
