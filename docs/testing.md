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
from hinoki.testing import ASGIClient

# create_app and TEST_DATABASE_URL come from the application under test.
async with ASGIClient(create_app(TEST_DATABASE_URL), strict_errors=True) as client:
    response = await client.post("/tasks", json={"title": "Write docs"}, token="member")
    assert response.status == 201
```

Its context manager runs module lifespans and the application's startup and shutdown hooks. If the application declares a database, point it to a disposable PostgreSQL database. `strict_errors=True` catches an HTTP error returned by an action but missing from its `errors=` declaration.

Both clients have `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and `query` (the HTTP `QUERY` method; its body goes in `json=` or `body=`, while the `query=` option of every call is the query string), plus `request(method, path, ...)` for any other method.

## Test file uploads

`ASGIClient` and `SyncClient` accept `data=` and `files=` and build the multipart boundary and `Content-Type` header for you. Use a sequence of file tuples to send repeated field names in order:

```python
async with ASGIClient(create_app()) as client:
    response = await client.post(
        "/documents",
        data={"title": "Report"},
        files=[("document", "report.txt", b"contents", "text/plain")],
    )
    assert response.status == 201
```

Each file tuple is `(field_name, filename, content_bytes, content_type)`. Form data may be a mapping or an ordered sequence of `(name, text)` pairs; sequences retain repeated names. Do not pass `json=` or raw `body=` together with `data=`/`files=`; the helper owns the multipart content type and boundary. The application enforces its configured complete-body and per-part limits. This helper sends complete in-memory bodies and does not model streaming uploads.

`headers=` accepts a mapping or a sequence of `(name, value)` pairs. Use pairs to send a repeated header; Hinoki's request header mapping joins repeated values with `", "`. A client can add a default `Host` header to each request:

```python
async with ASGIClient(create_app(), host="api.example.com") as client:
    response = await client.get(
        "/health", headers=[("X-Trace", "first"), ("X-Trace", "second")]
    )
```

The default remains no `Host` header. Set `headers={"host": "other.example.com"}` on a call to override the client's host. Because the clients run in-process, they do not log the startup warning about the default `allowed_hosts = *` that a served worker logs ([settings](settings.md#allowed_hosts)).

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

PostgreSQL-dependent Python and Rust tests require `HINOKI_TEST_DATABASE_URL`; without it those tests are skipped. Migration conformance tests for MySQL and MariaDB also require `HINOKI_TEST_MYSQL_URL` and `HINOKI_TEST_MARIADB_URL`, respectively. CI provides disposable real-server services. See [toolchain and CI](toolchain.md) for packaging and docs checks too.
