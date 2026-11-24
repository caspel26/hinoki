"""Application session binding, cookie lifecycle, authentication and CSRF."""

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from hinoki import Access, BearerAuth, Caller, Hinoki, ModuleContext, Secret, http
from hinoki.ai.mcpapp import select
from hinoki.auth import BaseAuth, Credentials
from hinoki.db import Database
from hinoki.errors import RegistrationError, Unauthorized
from hinoki.sessions import (
    DatabaseSessionStore,
    MemorySessionStore,
    Session,
    SessionAuth,
    SessionCapacityExceeded,
    SessionModule,
    SessionRecord,
    SessionSettings,
    SessionStore,
    SessionTooLarge,
    SessionUnavailable,
    _session_upsert,
)
from hinoki.testing import ASGIClient

SETTINGS = SessionSettings(csrf_secret=b"t" * 32)
STORE = MemorySessionStore()
SERVICE_TOKEN = "service"  # noqa: S105 - synthetic test credential


@http.post("/session/write")
async def write_session(session: Session) -> dict[str, bool]:
    session["value"] = "stored"
    return {"ok": True}


@http.get("/session/read")
async def read_session(session: Session) -> dict[str, object]:
    return {"value": session.get("value"), "csrf": session.csrf_token()}


@http.get("/session/sync")
def sync_session(session: Session) -> dict[str, bool]:
    session["sync"] = True
    return {"ok": True}


@http.post("/session/fail")
async def failed_session(session: Session) -> dict[str, bool]:
    session["discard"] = True
    raise RuntimeError("handler failed")


@http.get("/session/nested-fail")
async def failed_nested_session(session: Session) -> dict[str, bool]:
    profile = session.get("profile")
    assert isinstance(profile, dict)
    profile["name"] = "Mallory"
    raise RuntimeError("handler failed after nested read")


@http.post("/session/recover-set")
async def recover_set(session: Session) -> dict[str, bool]:
    with suppress(SessionTooLarge):
        session["large"] = "x" * 128
    with suppress(TypeError):
        session["invalid"] = object()
    external = {"items": [1]}
    session["copied"] = external
    external["items"].append(2)
    session["valid"] = "ok"
    return {"ok": True}


@http.post("/session/rotate")
async def rotate_session(session: Session) -> dict[str, bool]:
    await session.rotate()
    session["rotated"] = True
    return {"ok": True}


@http.post("/session/invalidate")
async def invalidate_session(session: Session) -> dict[str, bool]:
    await session.invalidate()
    return {"ok": True}


@http.post("/session/rotate-fail")
async def rotate_then_fail(session: Session) -> dict[str, bool]:
    await session.rotate()
    raise RuntimeError("failed after rotation")


def resolve_user(data: Mapping[str, object]) -> Caller[str] | None:
    if data.get("user") == "alice":
        return Caller("alice", "Alice")
    return None


@http.get("/session/me", access=Access(auth=SessionAuth(resolve_user)))
async def me(caller: Caller[str]) -> dict[str, str]:
    return {"user": caller.user}


async def test_cookie_round_trip_and_csrf_protection() -> None:
    app = Hinoki(
        include=[write_session, read_session, sync_session],
        modules=[SessionModule(STORE, SETTINGS)],
    )
    async with ASGIClient(app) as client:
        written = await client.post("/session/write")
        assert written.status == 200
        cookie = written.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
        token = cookie.split("=", 1)[1].split(";", 1)[0]
        read = await client.get(
            "/session/read", headers={"cookie": f"hinoki_session={token}"}
        )
        assert read.body["value"] == "stored"
        assert isinstance(read.body["csrf"], str)
        rejected = await client.post(
            "/session/write", headers={"cookie": f"hinoki_session={token}"}
        )
        assert rejected.status == 403
        accepted = await client.post(
            "/session/write",
            headers={
                "cookie": f"hinoki_session={token}",
                "x-csrf-token": read.body["csrf"],
                "origin": "http://testserver",
                "host": "testserver",
            },
        )
        assert accepted.status == 200
        sync_reply = await client.get("/session/sync")
        assert sync_reply.body == {"ok": True}


async def test_session_mutation_is_discarded_when_handler_raises() -> None:
    store = MemorySessionStore()
    app = Hinoki(include=[failed_session], modules=[SessionModule(store, SETTINGS)])
    async with ASGIClient(app) as client:
        response = await client.post("/session/fail")
        assert response.status == 500
        assert "set-cookie" not in response.headers
        assert not store._records


async def test_nested_session_read_cannot_mutate_store_before_commit() -> None:
    store = MemorySessionStore()
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await store.save(
        hashlib.sha256(token.encode()).hexdigest(),
        SessionRecord(
            {"profile": {"name": "Alice"}},
            now,
            now + timedelta(days=1),
            now + timedelta(hours=1),
            now,
        ),
    )
    app = Hinoki(
        include=[failed_nested_session], modules=[SessionModule(store, SETTINGS)]
    )
    async with ASGIClient(app) as client:
        response = await client.get(
            "/session/nested-fail",
            headers={"cookie": f"hinoki_session={token}"},
        )
    assert response.status == 500
    record = await store.load(hashlib.sha256(token.encode()).hexdigest())
    assert record is not None and record.data == {"profile": {"name": "Alice"}}


async def test_rotation_and_invalidation_change_cookie_ownership() -> None:
    store = MemorySessionStore()
    app = Hinoki(
        include=[
            write_session,
            rotate_session,
            invalidate_session,
            rotate_then_fail,
            read_session,
        ],
        modules=[SessionModule(store, SETTINGS)],
    )
    async with ASGIClient(app) as client:
        first = await client.post("/session/write")
        old = first.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        old_session = await client.get(
            "/session/read", headers={"cookie": f"hinoki_session={old}"}
        )
        failed = await client.post(
            "/session/rotate-fail",
            headers={
                "cookie": f"hinoki_session={old}",
                "x-csrf-token": old_session.body["csrf"],
            },
        )
        assert failed.status == 500
        assert await store.load(hashlib.sha256(old.encode()).hexdigest()) is not None
        rotated = await client.post(
            "/session/rotate",
            headers={
                "cookie": f"hinoki_session={old}",
                "x-csrf-token": old_session.body["csrf"],
            },
        )
        new = rotated.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        assert old != new
        old_hash = hashlib.sha256(old.encode()).hexdigest()
        assert await store.load(old_hash) is None
        new_session = await client.get(
            "/session/read", headers={"cookie": f"hinoki_session={new}"}
        )
        invalid = await client.post(
            "/session/invalidate",
            headers={
                "cookie": f"hinoki_session={new}",
                "x-csrf-token": new_session.body["csrf"],
            },
        )
        assert "Max-Age=0" in invalid.headers["set-cookie"]


async def test_session_auth_and_openapi_mcp_exclusion() -> None:
    store = MemorySessionStore()
    app = Hinoki(include=[write_session, me], modules=[SessionModule(store, SETTINGS)])
    async with ASGIClient(app) as client:
        written = await client.post("/session/write")
        token = written.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        # Authentication resolves a caller from the saved session data.
        session = Session(store, SETTINGS)
        await session.rotate()
        session["user"] = "alice"
        await session.commit()
        auth_token = session.token
        assert auth_token is not None
        response = await client.get(
            "/session/me", headers={"cookie": f"hinoki_session={auth_token}"}
        )
        assert response.body == {"user": "Alice"}
        assert token != auth_token

    operation = app.openapi()["paths"]["/session/write"]["post"]
    assert "parameters" not in operation
    tools = select(app, ["*.write_session"], True, "/mcp")
    assert tools[0].input_schema["properties"] == {}


def test_memory_store_is_bounded() -> None:
    async def run() -> None:
        store = MemorySessionStore(max_sessions=1)
        session = Session(store, SETTINGS)
        await session.rotate()
        session["key"] = "one"
        await session.commit()
        other = Session(store, SETTINGS)
        await other.rotate()
        other["key"] = "two"
        with pytest.raises(SessionCapacityExceeded):
            await other.commit()
        assert len(store._records) == 1

    asyncio.run(run())


async def test_expiry_and_idle_touch_use_deterministic_clock() -> None:
    store = MemorySessionStore()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    record = SessionRecord(
        {"k": "v"},
        now - timedelta(days=1),
        now + timedelta(days=10),
        now + timedelta(hours=1),
        now - timedelta(seconds=61),
    )
    await store.save(token_hash, record)
    session = await Session.from_cookie(store, token, SETTINGS, now=now)
    assert session.touched
    cookie = await session.commit(now)
    assert cookie is not None and "Max-Age=86400" in cookie[1]
    refreshed = await store.load(token_hash)
    assert refreshed is not None and refreshed.last_seen_at == now

    expired_token = secrets.token_urlsafe(32)
    expired_hash = hashlib.sha256(expired_token.encode()).hexdigest()
    await store.save(
        expired_hash,
        SessionRecord(
            {},
            now - timedelta(days=2),
            now - timedelta(seconds=1),
            now - timedelta(seconds=1),
            now - timedelta(days=1),
        ),
    )
    expired = await Session.from_cookie(store, expired_token, SETTINGS, now=now)
    assert expired.is_new


async def test_inflight_session_cannot_restore_a_revoked_token() -> None:
    store = MemorySessionStore()
    seed = Session(store, SETTINGS)
    await seed.rotate()
    seed["user"] = "alice"
    await seed.commit()
    assert seed.token is not None
    first = await Session.from_cookie(store, seed.token, SETTINGS)
    concurrent = await Session.from_cookie(store, seed.token, SETTINGS)
    await first.invalidate()
    await first.commit()
    concurrent["user"] = "mallory"
    with pytest.raises(SessionUnavailable):
        await concurrent.commit()
    assert await store.load(hashlib.sha256(seed.token.encode()).hexdigest()) is None


async def test_rotations_revoke_only_the_original_token() -> None:
    store = MemorySessionStore()
    seed = Session(store, SETTINGS)
    await seed.rotate()
    await seed.commit()
    original = seed.token
    assert original is not None

    first = await Session.from_cookie(store, original, SETTINGS)
    await first.rotate()
    await first.invalidate()
    await first.commit()
    assert await store.load(hashlib.sha256(original.encode()).hexdigest()) is None

    second_seed = Session(store, SETTINGS)
    await second_seed.rotate()
    await second_seed.commit()
    original_after_rotation = second_seed.token
    assert original_after_rotation is not None
    second = await Session.from_cookie(store, original_after_rotation, SETTINGS)
    await second.rotate()
    intermediate = second.token
    await second.rotate()
    await second.commit()
    assert (
        await store.load(hashlib.sha256(original_after_rotation.encode()).hexdigest())
        is None
    )
    assert intermediate is not None
    assert await store.load(hashlib.sha256(intermediate.encode()).hexdigest()) is None
    assert second.token is not None
    assert (
        await store.load(hashlib.sha256(second.token.encode()).hexdigest()) is not None
    )


async def test_rejected_session_values_do_not_poison_successful_commit() -> None:
    store = MemorySessionStore()
    settings = SessionSettings(csrf_secret=b"t" * 32, max_bytes=64)
    app = Hinoki(include=[recover_set], modules=[SessionModule(store, settings)])
    async with ASGIClient(app) as client:
        response = await client.post("/session/recover-set")
    assert response.status == 200
    token = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    record = await store.load(hashlib.sha256(token.encode()).hexdigest())
    assert record is not None
    assert record.data == {"copied": {"items": [1]}, "valid": "ok"}


def test_handler_cannot_request_session_twice() -> None:
    def duplicate(first: Session, second: Session) -> dict[str, bool]:
        return {"same": first is second}

    with pytest.raises(RegistrationError, match="may request Session only once"):
        Hinoki(include=[http.get("/duplicate")(duplicate)])


async def test_session_csrf_normalizes_origin_and_rejects_same_site_fetch() -> None:
    store = MemorySessionStore()
    app = Hinoki(
        include=[write_session, read_session], modules=[SessionModule(store, SETTINGS)]
    )
    async with ASGIClient(app) as client:
        created = await client.post("/session/write")
        token = created.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
        read = await client.get(
            "/session/read", headers={"cookie": f"hinoki_session={token}"}
        )
        headers = {
            "cookie": f"hinoki_session={token}",
            "x-csrf-token": read.body["csrf"],
            "origin": "http://testserver",
            "host": "TESTSERVER:80",
            "sec-fetch-site": "same-origin",
        }
        accepted = await client.post("/session/write", headers=headers)
        assert accepted.status == 200
        rejected = await client.post(
            "/session/write",
            headers={**headers, "sec-fetch-site": "same-site"},
        )
        assert rejected.status == 403


async def test_database_session_store_upsert_revoke_and_subject_delete(
    tmp_path: object,
) -> None:
    database = await Database.connect(f"sqlite://{tmp_path}/sessions.sqlite3")
    try:
        await database.execute(
            "CREATE TABLE hinoki_sessions (token_hash varchar(64) PRIMARY KEY, "
            "data text NOT NULL, created_at text NOT NULL, expires_at text NOT NULL, "
            "idle_expires_at text NOT NULL, last_seen_at text NOT NULL, "
            "subject varchar(255), revoked integer NOT NULL DEFAULT 0)"
        )
        store = DatabaseSessionStore(database)
        now = datetime.now(UTC)
        token_hash = "a" * 64
        record = SessionRecord(
            {"name": "alice"},
            now,
            now + timedelta(days=2),
            now + timedelta(days=1),
            now,
            "alice",
        )
        await store.save(token_hash, record)
        assert await store.load(token_hash) == record
        assert await store.delete_by_subject("alice") == 1
        assert await store.load(token_hash) is None
        with pytest.raises(SessionUnavailable, match="revoked session"):
            await store.save(token_hash, record)
        assert await store.load(token_hash) is None
    finally:
        database.close()


async def test_database_session_purge_removes_expired_including_revoked(
    tmp_path: object,
) -> None:
    database = await Database.connect(f"sqlite://{tmp_path}/purge-sessions.sqlite3")
    try:
        await database.execute(
            "CREATE TABLE hinoki_sessions (token_hash varchar(64) PRIMARY KEY, "
            "data text NOT NULL, created_at text NOT NULL, expires_at text NOT NULL, "
            "idle_expires_at text NOT NULL, last_seen_at text NOT NULL, "
            "subject varchar(255), revoked integer NOT NULL DEFAULT 0)"
        )
        store = DatabaseSessionStore(database)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        expired_hash, live_hash = "e" * 64, "l" * 64
        expired = SessionRecord(
            {},
            now - timedelta(days=2),
            now - timedelta(seconds=1),
            now - timedelta(seconds=1),
            now - timedelta(days=1),
        )
        live = SessionRecord(
            {},
            now,
            now + timedelta(days=1),
            now + timedelta(hours=1),
            now,
        )
        await store.save(expired_hash, expired)
        await store.delete(expired_hash)
        await store.save(live_hash, live)
        assert await store.purge_expired(now) == 1
        assert await store.load(expired_hash) is None
        assert await store.load(live_hash) == live
    finally:
        database.close()


def test_database_upsert_syntax_supports_project_dialects() -> None:
    assert "ON CONFLICT" in _session_upsert("postgres")
    assert "ON DUPLICATE KEY" in _session_upsert("mysql")


async def test_session_resource_requires_module_startup() -> None:
    @http.get("/unprovided")
    async def endpoint(session: Session) -> dict[str, bool]:
        return {"new": session.is_new}

    with pytest.raises(RuntimeError, match="SessionStore"):
        async with ASGIClient(Hinoki(include=[endpoint])):
            pytest.fail("missing SessionModule should fail startup")


async def test_memory_store_forgets_revoked_sessions_and_frees_capacity() -> None:
    store = MemorySessionStore(max_sessions=2)
    now = datetime.now(UTC)

    def record(subject: str | None = None) -> SessionRecord:
        return SessionRecord(
            {"secret": "payload"},
            now,
            now + timedelta(days=1),
            now + timedelta(hours=1),
            now,
            subject,
        )

    await store.save("a" * 64, record())
    await store.save("b" * 64, record("alice"))
    await store.delete("a" * 64)
    assert await store.delete_by_subject("alice") == 1
    assert not store._records
    # Revoked tokens stay unrestorable without holding a capacity slot.
    for revoked in ("a" * 64, "b" * 64):
        with pytest.raises(SessionUnavailable, match="revoked"):
            await store.save(revoked, record())
    await store.save("c" * 64, record())
    await store.save("d" * 64, record())
    # Revocations are bounded too: the oldest is dropped first.
    await store.delete("c" * 64)
    assert list(store._revoked) == ["b" * 64, "c" * 64]


async def test_memory_store_prunes_idle_sessions_and_expired_revocations() -> None:
    store = MemorySessionStore(max_sessions=1)
    now = datetime.now(UTC)
    await store.save(
        "i" * 64,
        SessionRecord(
            {"secret": "payload"},
            now,
            now + timedelta(days=1),
            now + timedelta(minutes=1),
            now,
        ),
    )
    assert await store.purge_expired(now + timedelta(minutes=2)) == 0
    assert not store._records and "i" * 64 in store._revoked
    assert await store.load("i" * 64) is None
    await store.purge_expired(now + timedelta(days=2))
    assert not store._revoked


def test_session_module_requires_settings() -> None:
    with pytest.raises(TypeError, match="SessionSettings"):
        SessionModule(MemorySessionStore(), None)  # type: ignore[arg-type]


class Tokens(BearerAuth[str]):
    def resolve_token(self, token: str) -> Caller[str] | None:
        return Caller("service", "Service") if token == SERVICE_TOKEN else None


class EitherAuth(BaseAuth[str]):
    """An application provider composed of a bearer and a session provider."""

    def __init__(self) -> None:
        self.providers = (Tokens(), SessionAuth(resolve_user))

    async def authenticate(self, credentials: Credentials) -> Caller[str] | None:
        for provider in self.providers:
            with suppress(Unauthorized):
                found = await provider.authenticate(credentials)
                if found is not None:
                    return found
        return None


@http.post("/session/composite", access=Access(auth=EitherAuth()))
async def composite(caller: Caller[str]) -> dict[str, str]:
    return {"user": caller.user}


async def signed_in(store: MemorySessionStore) -> str:
    session = Session(store, SETTINGS)
    await session.rotate()
    session["user"] = "alice"
    await session.commit()
    assert session.token is not None
    return session.token


async def test_composite_auth_loads_the_session_and_enforces_csrf() -> None:
    store = MemorySessionStore()
    app = Hinoki(include=[composite], modules=[SessionModule(store, SETTINGS)])
    async with ASGIClient(app) as client:
        token = await signed_in(store)
        cookie = {"cookie": f"hinoki_session={token}"}
        forged = await client.post("/session/composite", headers=cookie)
        assert forged.status == 403
        csrf = Session(store, SETTINGS, token).csrf_token()
        accepted = await client.post(
            "/session/composite", headers={**cookie, "x-csrf-token": csrf}
        )
        assert accepted.status == 200
        assert accepted.body == {"user": "Alice"}
        bearer = await client.post(
            "/session/composite", headers={"authorization": "Bearer service"}
        )
        assert bearer.body == {"user": "Service"}


@http.websocket("/session/socket", access=Access(auth=SessionAuth(resolve_user)))
async def session_socket(socket: http.WebSocket, caller: Caller[str]) -> None:
    await socket.send_text(caller.user)


async def test_session_auth_admits_a_websocket_handshake() -> None:
    store = MemorySessionStore()
    app = Hinoki(include=[session_socket], modules=[SessionModule(store, SETTINGS)])
    async with ASGIClient(app) as client:
        token = await signed_in(store)

        async def handshake(cookie: str) -> list[dict[str, Any]]:
            inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            inbox.put_nowait({"type": "websocket.connect"})
            inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
            sent: list[dict[str, Any]] = []

            async def send(event: dict[str, Any]) -> None:
                sent.append(event)

            scope = {
                "type": "websocket",
                "path": "/session/socket",
                "query_string": b"",
                "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
                "subprotocols": [],
            }
            await asyncio.wait_for(client.adapter(scope, inbox.get, send), 5)
            return sent

        admitted = await handshake(f"hinoki_session={token}")
        assert admitted[0]["type"] == "websocket.accept"
        assert admitted[1] == {"type": "websocket.send", "text": "Alice"}
        refused = await handshake("hinoki_session=unknown")
        assert [event["type"] for event in refused] == ["websocket.close"]


async def test_session_websocket_requires_the_session_module() -> None:
    with pytest.raises(RuntimeError, match="SessionStore"):
        async with ASGIClient(Hinoki(include=[session_socket])):
            pytest.fail("missing SessionModule should fail startup")


@dataclass(frozen=True, slots=True)
class SlottedSessionAuth(BaseAuth[str]):
    """A slotted application provider wrapping the session provider."""

    inner: SessionAuth[str]

    async def authenticate(self, credentials: Credentials) -> Caller[str] | None:
        return await self.inner.authenticate(credentials)


@http.post(
    "/session/slotted",
    access=Access(auth=SlottedSessionAuth(SessionAuth(resolve_user))),
)
async def slotted(caller: Caller[str]) -> dict[str, str]:
    return {"user": caller.user}


async def test_a_slotted_composite_auth_loads_the_session_and_checks_csrf() -> None:
    store = MemorySessionStore()
    token = await signed_in(store)
    app = Hinoki(include=[slotted], modules=[SessionModule(store, SETTINGS)])
    async with ASGIClient(app) as client:
        cookie = {"cookie": f"hinoki_session={token}"}
        refused = await client.post("/session/slotted", headers=cookie)
        assert refused.status == 403
        csrf = Session(store, SETTINGS, token).csrf_token()
        allowed = await client.post(
            "/session/slotted", headers={**cookie, "x-csrf-token": csrf}
        )
        assert allowed.status == 200
        assert allowed.body == {"user": "Alice"}


async def test_settings_take_a_secret_and_the_module_provides_the_store_type() -> None:
    settings = SessionSettings(csrf_secret=Secret("t" * 32))
    assert settings.csrf_key() == b"t" * 32
    assert "t" * 32 not in repr(settings)
    with pytest.raises(ValueError, match="32-byte CSRF secret"):
        SessionSettings(csrf_secret=Secret("short"))
    store = MemorySessionStore()
    ctx = ModuleContext()
    async with SessionModule(store, settings).lifespan(ctx):
        assert ctx.get(MemorySessionStore) is store
        assert ctx.get(SessionStore) is store
