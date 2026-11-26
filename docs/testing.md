---
status: implemented
related:
  - sync-async.md
  - endpoints.md
  - function-endpoints.md
  - new-project.md
---

# Test an application

A project created with `hinoki new` already has a test that runs a request
through the whole stack, without a network socket:

```python
# tests/healthtest.py
from hinoki.testing import ASGIClient

from notes.main import app


async def test_health_answers_through_the_whole_stack() -> None:
    async with ASGIClient(app) as client:
        reply = await client.get("/health")
    assert (reply.status, reply.body) == (200, {"status": "ok"})
```

Run it with `uv run pytest`. The generated `pyproject.toml` sets
`asyncio_mode = "auto"` and collects files named `*test.py`.

`ASGIClient` runs the application's startup (modules, database pool, startup
hooks and [rules](rules.md)) when the block opens and its shutdown when it
closes. A reply has `status`, `headers`, `body` (decoded JSON, or text) and `raw`
bytes.

## Test protected actions

`token=` sends `Authorization: Bearer <token>`, so a test exercises the real
provider and policy:

```python
# tests/billingtest.py
from hinoki.testing import ASGIClient

from notes.main import app


async def test_invoices_need_a_staff_token() -> None:
    async with ASGIClient(app) as client:
        anonymous = await client.get("/billing/invoices")
        unknown = await client.get("/billing/invoices", token="not-a-token")
    assert anonymous.status == 401
    assert unknown.status == 401
```

## Use a disposable database

The application reads its settings when its module is imported, so point the
prefix variable at a throwaway database first. A `conftest.py` at the project
root is imported before any test module:

```python
# conftest.py
import os

os.environ["NOTES_DATABASE__URL"] = "sqlite://./.hinoki/test.sqlite3"
```

This applies when the settings declare `database: settings.DatabaseSettings`.
Create the tables once, with the same variable:

```sh
NOTES_DATABASE__URL=sqlite://./.hinoki/test.sqlite3 uv run hinoki migrations apply
```

When `settings.toml` sets `engine` and its connection fields, a URL conflicts
with them (`set either url or engine with its fields, not both`). Override the
fields instead, and clear the server-only ones SQLite refuses:

```python
# conftest.py
import os

os.environ.update(
    NOTES_DATABASE__ENGINE="sqlite",
    NOTES_DATABASE__NAME=".hinoki/test.sqlite3",
    NOTES_DATABASE__HOST="",
    NOTES_DATABASE__USER="",
)
```

Apply the migrations with the same variables, and pass the same keys under
`"database"` to `overrides=` in the example below.

When one test needs other settings, build a second application from the same
code instead of changing the shared one. Give `main.py` a `create_app(settings)`
factory, as [`examples/notes/notes/main.py`](https://github.com/caspel26/hinoki/blob/main/examples/notes/notes/main.py)
does (`create_app`, line 12). The factory lives in the module `hinoki.toml`
names as `project.application`, so `Hinoki(...)` fills routes, events,
middleware and rules from the manifest. This example only calls `/health`, so
its new database needs no tables; to give a test migrated tables, copy them as
[below](#give-each-test-its-own-database):

```python
# tests/settingstest.py
from hinoki.testing import ASGIClient

from notes.main import create_app
from notes.settings import ProjectSettings


async def test_health_with_its_own_database() -> None:
    settings = ProjectSettings.load(
        overrides={"database": {"url": "sqlite://./.hinoki/other.sqlite3"}}
    )
    async with ASGIClient(create_app(settings)) as client:
        assert (await client.get("/health")).status == 200
```

A `Hinoki(...)` built in any other module (a test, a script) does not read the
manifest; pass what you want explicitly (or use `manifest_routes()`,
`manifest_events()`, `manifest_middleware()` and `manifest_rules()` from
`hinoki.project`).

Use synthetic rows and credentials, and never point test settings at production
data.

### Give each test its own database

The tests above share one file, so rows one test writes are still there in the
next. To start every test from empty, migrated tables, migrate a database in the
test's temporary directory with the app's own migrations and build the
application on it, as
[`examples/notes/notes_tests/conftest.py`](https://github.com/caspel26/hinoki/blob/main/examples/notes/notes_tests/conftest.py) does:

```python
# tests/conftest.py
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from hinoki.db import Database
from hinoki.orm import migrator
from hinoki.testing import ASGIClient

from notes.main import create_app
from notes.settings import ProjectSettings

MIGRATIONS = Path(__file__).parents[1] / "notes" / "apps" / "notes" / "migrations"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[ASGIClient]:
    url = f"sqlite://{tmp_path / 'test.sqlite3'}"
    database = await Database.connect(url, max_size=1)
    await migrator.apply(database, MIGRATIONS)
    database.close()
    settings = ProjectSettings.load(overrides={"database": {"url": url}})
    async with ASGIClient(create_app(settings)) as client:
        yield client
```

A test takes `client` as an argument; whatever it writes is gone in the next
test. The migrations come from the app's own directory, so a new migration is
picked up without extra steps. In a project with several apps, apply each app's
migration directory in dependency order. The fixture builds SQLite files only.

## Test email

With `email: settings.EmailSettings` set to `backend=EmailBackend.MEMORY`, every sent message is kept in the worker's `mailer.outbox` and nothing leaves the process; `outbox` entries carry the `Message` and the rendered bytes. A sample test is in [send email](email.md#test-with-the-memory-backend). For SMTP behaviour use a fake server on `127.0.0.1` and synthetic `example.com` addresses. To test a template without sending, call `template.render(context)` (it returns the three parts) or run `hinoki mail preview`; see [email templates](email-templates.md).

## Catch undeclared errors

`ASGIClient(app, strict_errors=True)` fails the test when an action answers an
HTTP error that is missing from its `errors=` declaration, so the OpenAPI
document cannot drift from what the action really does.

`ASGIClient` and `SyncClient` (for synchronous tests, [below](#synchronous-tests-and-time)) both have `get`, `post`, `put`, `patch`, `delete`, `head`, `options` and
`query` (the HTTP `QUERY` method; its body goes in `json=` or `body=`, while the
`query=` option of every call is the query string), plus
`request(method, path, ...)` for any other method.

The path is only the path: a `?` or `#` in it raises `ValueError` instead of being routed as part of the path (which would answer a 404 for the wrong reason). Send the query string with `query=`:

```python
reply = await client.get("/items", query="limit=10&offset=20")
```

## Test file uploads

`ASGIClient` and `SyncClient` accept `data=` and `files=` and build the multipart boundary and `Content-Type` header for you. Use a sequence of file tuples to send repeated field names in order:

```python
# tests/uploadtest.py
async with ASGIClient(app) as client:
    response = await client.post(
        "/documents",
        data={"title": "Report"},
        files=[("document", "report.txt", b"contents", "text/plain")],
    )
    assert response.status == 201
```

Each file tuple is `(field_name, filename, content_bytes, content_type)`. Form data may be a mapping or an ordered sequence of `(name, text)` pairs; sequences retain repeated names. Do not pass `json=` or raw `body=` together with `data=`/`files=`; the helper owns the multipart content type and boundary. The application enforces its configured complete-body and per-part limits. This helper sends complete in-memory bodies and does not model streaming uploads. For streamed responses see [test streams](#test-streams).

## Headers and hosts

`headers=` accepts a mapping or a sequence of `(name, value)` pairs. Use pairs to send a repeated header; Hinoki's request header mapping joins repeated values with `", "`. A client can add a default `Host` header to each request:

```python
# tests/headerstest.py
async with ASGIClient(app, host="api.example.com") as client:
    response = await client.get(
        "/health", headers=[("X-Trace", "first"), ("X-Trace", "second")]
    )
```

The default remains no `Host` header. Set `headers={"host": "other.example.com"}` on a call to override the client's host. Because the clients run in-process, they do not log the startup warning about the default `allowed_hosts = *` that a served worker logs ([Hosts, CORS and HTTPS](web-security.md#allowed_hosts)).

## Test streams

`client.get()` waits for the end of a streamed response and returns the whole body. To see the head and the pieces as they are sent, use `client.stream()`; leaving the block tells the application that the client disconnected, so the source of the stream is closed:

```python
# tests/eventstest.py
from hinoki.testing import ASGIClient

from notes.main import app


async def test_events_arrive_one_by_one() -> None:
    async with ASGIClient(app) as client:
        async with client.stream("GET", "/notes/events", token="t") as reply:
            assert reply.status == 200
            assert reply.headers["content-type"].startswith("text/event-stream")
            first = await anext(reply.chunks())
```

`reply.chunks()` yields each piece as the application sent it, and raises the error that cut the response, if one did. `reply.read()` collects the rest. `SyncClient.stream()` is the same with `with` and a plain iterator. A stream that never ends makes `client.get()` fail with `BodyTooLarge` once the body passes `max_bytes` (10 MiB; pass `max_bytes=` to change it), so a test cannot hang on it. The in-process client runs the ASGI pump, not Kiso's: it shows what the client receives and when the source is closed, not the server's queue, timeouts or keep-alive.

## Synchronous tests and time

`SyncClient` is available for synchronous test functions. It runs the app's lifespan and refuses unsafe nested use from a running event loop.

For deterministic time, `ManualClock` is a thread-safe callable clock. It accepts and returns timezone-aware instants normalized to UTC; `advance(seconds=...)` and `set(datetime)` control its value. A naive datetime raises `ValueError`. Pass it to `app.now(clock)` or other code that accepts a `Clock`.

## Test the layers separately

Keep business operations independent of `Request` and HTTP response types when practical. Test ownership and state transitions against a disposable database, then test route binding, access, status codes and error declarations through `ASGIClient`.

## Repository checks

From the checkout, the documented gates are:

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked complexipy . --failed
uv run --locked pytest --cov
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
uv run --locked --group docs mkdocs build --strict
```

PostgreSQL-dependent Python and Rust tests require `HINOKI_TEST_DATABASE_URL`; without it those tests are skipped. Migration conformance tests for MySQL and MariaDB also require `HINOKI_TEST_MYSQL_URL` and `HINOKI_TEST_MARIADB_URL`, respectively. CI provides disposable real-server services. See [toolchain and CI](toolchain.md) for the Rust complexity check, the pre-commit hooks and the packaging checks.

## See also

- [Create a project](new-project.md): what the generated tests and settings contain.
- [Sync and async](sync-async.md)
- [Testing API reference](reference/testing.md)
