---
status: implemented
related:
  - sync-async.md
  - endpoints.md
---

# Testing

Hinoki has two useful levels of application tests: call the operation functions directly, then exercise the app through its ASGI interface.

## Test the application over ASGI

`ASGIClient` drives startup, HTTP requests and shutdown without opening a network socket:

```python
async with ASGIClient(create_app(TEST_DATABASE_URL), strict_errors=True) as client:
    response = await client.post("/tasks", json={"title": "Write docs"}, token="member")
    assert response.status == 201
```

Its context manager runs module lifespans and the application's startup and shutdown hooks. If the application has a `DatabaseModule`, point it to a disposable PostgreSQL database. `strict_errors=True` catches an HTTP error returned by an action but missing from its `errors=` declaration.

Both clients have `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and `query` (the HTTP `QUERY` method; its body goes in `json=` or `body=`, while the `query=` option of every call is the query string), plus `request(method, path, ...)` for any other method.

`headers=` accepts a mapping or a sequence of `(name, value)` pairs. Use pairs to send a repeated header; Hinoki's request header mapping joins repeated values with `", "`. A client can add a default `Host` header to each request:

```python
async with ASGIClient(create_app(), host="api.example.com") as client:
    response = await client.get(
        "/health", headers=[("X-Trace", "first"), ("X-Trace", "second")]
    )
```

The default remains no `Host` header. Set `headers={"host": "other.example.com"}` on a call to override the client's host.

`SyncClient` is available for synchronous test functions. It runs the app's lifespan and refuses unsafe nested use from a running event loop.

For deterministic time, `ManualClock` is a thread-safe callable clock. It accepts and returns timezone-aware instants normalized to UTC; `advance(seconds=...)` and `set(datetime)` control its value. A naive datetime raises `ValueError`. Pass it to `app.now(clock)` or other code that accepts a `Clock`.

## Test the layers separately

Keep business operations independent of `Request` and HTTP response types when practical. Test ownership and state transitions against a disposable database, then test route binding, access, status codes and error declarations through `ASGIClient`.

Use synthetic rows and credentials. Never point test settings at production data.

## Repository checks

From the checkout, the documented gates are:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest --cov
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

Database-dependent Python and Rust tests require `HINOKI_TEST_DATABASE_URL`; without it those tests are skipped. CI provides a disposable PostgreSQL service. See [toolchain and CI](toolchain.md) for packaging and docs checks too.
