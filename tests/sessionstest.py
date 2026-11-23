"""Application session binding, cookie lifecycle, authentication and CSRF."""

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from hinoki import Access, Caller, Hinoki, http
from hinoki.ai.mcpapp import select
from hinoki.db import Database
from hinoki.sessions import (
    DatabaseSessionStore,
    MemorySessionStore,
    Session,
    SessionAuth,
    SessionCapacityExceeded,
    SessionModule,
    SessionRecord,
    SessionSettings,
    SessionUnavailable,
    _session_upsert,
)
from hinoki.testing import ASGIClient

SETTINGS = SessionSettings(csrf_secret=b"t" * 32)
STORE = MemorySessionStore()


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
        await store.save(token_hash, record)
        assert await store.load(token_hash) is None
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
