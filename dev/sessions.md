# Application sessions

Application sessions keep small JSON state on the server and identify it with a
random cookie token. The raw token is sent to the client; the store keeps only
its SHA-256 digest. This is separate from the optional admin's session table
and cookie.

Configure a store explicitly. The CSRF key must be at least 32 bytes; keep it in
a secret setting or environment variable, and configure the same value on every
worker:

```python
from collections.abc import Mapping

from hinoki import Access, Caller, Hinoki, http
from hinoki.sessions import (
    DatabaseSessionStore,
    Session,
    SessionAuth,
    SessionModule,
    SessionSettings,
)

session_settings = SessionSettings(csrf_secret=secret_from_environment)


@http.post("/login")
async def login(session: Session) -> dict[str, str]:
    user = await verify_credentials()
    await session.rotate()  # issue a fresh token after authentication
    session["user_id"] = str(user.id)
    session.subject = str(user.id)
    return {"status": "signed_in"}


def resolve_user(data: Mapping[str, object]) -> Caller[User] | None:
    user_id = data.get("user_id")
    return load_caller(user_id) if isinstance(user_id, str) else None


session_auth = SessionAuth(resolve_user)


@http.get("/me", access=Access(auth=session_auth))
async def me(caller: Caller[User]) -> UserSchema:
    return UserSchema.from_user(caller.user)


app = Hinoki(
    include=[login, me],
    modules=[SessionModule(DatabaseSessionStore(), session_settings)],
)
```

`csrf_secret` takes `bytes`, text or a `settings.Secret` (encoded as UTF-8) of at least 32 bytes; it is left out of the settings' `repr` and comparisons, and `settings.csrf_key()` returns the bytes.

`SessionAuth` resolves a `Caller` from the read-only session data and works with
`Access` and `Caller[T]`. It documents the configured cookie name as an OpenAPI
cookie security scheme. If `SessionSettings.cookie_name` is customized, pass the
same name to `SessionAuth(cookie_name=...)`. `SessionModule(store, settings)`
requires its `SessionSettings`.

A custom `Auth` that composes `SessionAuth` (for example, one that tries a bearer
token and then the session) is detected automatically: Hinoki loads the session
and applies the CSRF rules below. Set `uses_session = True` on an `Auth` class
that reads the session in another way.

`SessionAuth` also works on `@http.websocket` routes. The session is loaded
read-only during the handshake, and an unknown or expired cookie refuses the
connection. The handshake `Origin` is checked by the websocket origin rules.

## State and response lifecycle

`Session` is request-scoped. It provides `get`, item lookup and assignment,
`del session[key]`, `clear`, `rotate`, `invalidate`, `csrf_token`, `is_new`,
`modified` and `subject`. Values must be JSON serializable; the default encoded
payload limit is 16 KiB. `None` is a normal value inside the mapping. Nested
values returned by reads are copies; assign the updated value back through
`session[key]` to persist a nested change.

Hinoki loads a session when a route declares `session: Session` or uses
`SessionAuth`. Mutations are saved and `Set-Cookie` is added only after the
handler returns successfully (that is, without raising an exception) and after
any `atomic=True` SQL transaction has committed, so a session store failure
does not roll back an already-committed SQL transaction. A returned `Response`
with an error status is still a successful handler return. If the handler
raises, pending changes and cookie changes are discarded.

`rotate()` issues a fresh token and revokes the old one on successful response;
use it on login and privilege changes to prevent fixation. `invalidate()`
revokes the current token and sends a clearing cookie. Revocations are retained
until the session's absolute expiry, so an already-running request cannot
restore a revoked token with a stale write. `MemorySessionStore` drops a revoked
session's data immediately and keeps only a bounded record of revoked tokens.

The cookie defaults to `HttpOnly`, `SameSite=Lax`, `Secure`, and `Path=/`.
`cookie_secure=False` is available for local HTTP development. The absolute
timeout defaults to 14 days and the idle timeout to one day. A successful
request refreshes the idle expiry after the 60-second touch interval. Missing,
malformed and expired cookies start as an anonymous session; they do not emit a
cookie unless the handler changes the session.

## CSRF

Unsafe methods (`POST`, `PUT`, `PATCH`, `DELETE`, `QUERY`) on a route that uses a
session or `SessionAuth` require a valid `X-CSRF-Token` when the request carries
an existing session cookie. The token is an HMAC-SHA256 value bound to that
session; `session.csrf_token()` returns it for a frontend to place in the
header. When `Origin` is present it must match the request origin, and an
explicit `Sec-Fetch-Site: cross-site` or `none` is rejected. `GET`, `HEAD` and
`OPTIONS` are safe methods.

For example, a page or JSON bootstrap action can return `session.csrf_token()`;
the browser then sends that value in `X-CSRF-Token` for writes. A missing or
invalid token receives 403 `csrf_rejected`. Store failures fail closed with 503
`session_unavailable`; a session is never silently treated as anonymous when
the store is down.

The bootstrap action must establish a token before asking for the CSRF value:

```python
@http.get("/csrf")
async def csrf_bootstrap(session: Session) -> dict[str, str]:
    if session.is_new:
        await session.rotate()
    return {"csrf_token": session.csrf_token()}
```

The successful response persists the session and sends its cookie; subsequent
unsafe requests can send the returned CSRF token with that cookie.

## Stores, migrations and limits

| Store | Visibility and ownership | Capacity and cleanup |
| --- | --- | --- |
| `MemorySessionStore(max_sessions=10_000)` | Per worker process; workers do not share sessions | Bounded by session count; it removes expired sessions and returns 503 when full of live or revoked sessions |
| `DatabaseSessionStore()` | Shared by workers through the application's `Database` | No configured row cap; call `purge_expired()` from a scheduled maintenance task |

`DatabaseSessionStore` depends on `DatabaseModule` when it is not given a
database directly. It expects the `hinoki_sessions` table. Apply the SQL at
`migrations_path() / "0001-initial.sql"` through an explicit project migration
and review/apply that migration before starting the application. Importing the
module and starting the worker do not create or alter tables.
`purge_expired()` deletes rows past their absolute expiry, including revoked
token tombstones; revoked rows remain until that expiry.

The database store keeps JSON session data as plain text, so do not put secrets
in session values unless the application encrypts them before storage or the
database's at-rest protections meet its policy. Sessions identify a subject for
revocation with `delete_by_subject`; they do not implement user ownership or
authorization by themselves. The application still owns account checks and
permission policy. Database cleanup is explicit; Hinoki does not start a
background purge task.

`SessionTooLarge` is a 413 response. A malformed store row or database failure
is reported as `SessionUnavailable` (503) without exposing the token, SQL or
stored data. Session migrations and revocation are distinct from file storage
and cache migrations.
