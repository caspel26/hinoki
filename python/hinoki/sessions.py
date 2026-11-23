"""Server-side application sessions with opaque, hashed cookie tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar, cast

from hinoki.auth import BaseAuth, Caller, Credentials
from hinoki.blocking import call_hook
from hinoki.db import Database, DatabaseModule
from hinoki.errors import APIError, Unauthorized
from hinoki.http.response import set_cookie
from hinoki.modules import Module, ModuleContext, Resource
from hinoki.utils import utc_now

T = TypeVar("T")
log = logging.getLogger("hinoki.sessions")
MAX_SESSION_BYTES = 16 * 1024
MAX_COOKIE_TOKEN_BYTES = 128
MAX_SUBJECT_BYTES = 255
MIN_CSRF_SECRET_BYTES = 32
MYSQL_SESSION_UPSERT = (
    "INSERT INTO hinoki_sessions (token_hash, data, created_at, expires_at, "
    "idle_expires_at, last_seen_at, subject) VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON DUPLICATE KEY UPDATE data = IF(revoked = 0, VALUES(data), data), "
    "expires_at = IF(revoked = 0, VALUES(expires_at), expires_at), "
    "idle_expires_at = IF(revoked = 0, VALUES(idle_expires_at), idle_expires_at), "
    "last_seen_at = IF(revoked = 0, VALUES(last_seen_at), last_seen_at), "
    "subject = IF(revoked = 0, VALUES(subject), subject)"
)
SQLITE_SESSION_UPSERT = (
    "INSERT INTO hinoki_sessions (token_hash, data, created_at, expires_at, "
    "idle_expires_at, last_seen_at, subject) VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(token_hash) DO UPDATE SET data = excluded.data, "
    "expires_at = excluded.expires_at, idle_expires_at = excluded.idle_expires_at, "
    "last_seen_at = excluded.last_seen_at, subject = excluded.subject "
    "WHERE hinoki_sessions.revoked = 0"
)


class SessionError(APIError):
    """Base failure for session persistence and decoding."""

    status = 500
    code = "session_error"
    message = "Session handling failed."


class SessionUnavailable(SessionError):
    """The configured session store could not serve the request."""

    status = 503
    code = "session_unavailable"
    message = "Session storage is unavailable."


class SessionTooLarge(SessionError):
    """A serialized session exceeds its configured size limit."""

    status = 413
    code = "session_too_large"
    message = "The session exceeds its size limit."


class SessionCapacityExceeded(SessionUnavailable):
    """A memory session backend reached its configured number of live sessions."""

    code = "session_capacity_exceeded"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    data: dict[str, object]
    created_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    last_seen_at: datetime
    subject: str | None = None


def _copy_record(record: SessionRecord) -> SessionRecord:
    return SessionRecord(
        deepcopy(record.data),
        record.created_at,
        record.expires_at,
        record.idle_expires_at,
        record.last_seen_at,
        record.subject,
    )


class SessionStore(Resource):
    """Worker-owned store contract; records are keyed by token digest."""

    async def load(self, token_hash: str) -> SessionRecord | None:
        raise NotImplementedError

    async def save(self, token_hash: str, record: SessionRecord) -> None:
        raise NotImplementedError

    async def delete(self, token_hash: str) -> None:
        raise NotImplementedError

    async def delete_by_subject(self, subject: str) -> int:
        raise NotImplementedError

    async def purge_expired(self, now: datetime | None = None) -> int:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SessionSettings(Resource):
    cookie_name: str = "hinoki_session"
    cookie_secure: bool = True
    max_bytes: int = MAX_SESSION_BYTES
    absolute_timeout: timedelta = timedelta(days=14)
    idle_timeout: timedelta = timedelta(days=1)
    touch_seconds: int = 60
    csrf_secret: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.max_bytes < 1
            or self.idle_timeout <= timedelta(0)
            or self.touch_seconds < 1
        ):
            raise ValueError("session limits and idle timeout must be positive")
        if (
            self.absolute_timeout <= timedelta(0)
            or len(self.csrf_secret) < MIN_CSRF_SECRET_BYTES
        ):
            raise ValueError(
                "sessions require an absolute timeout and 32-byte CSRF secret"
            )


class MemorySessionStore(SessionStore):
    """Bounded, process-local store for tests and single-worker deployments."""

    def __init__(self, max_sessions: int = 10_000) -> None:
        if isinstance(max_sessions, bool) or max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.max_sessions = max_sessions
        self._records: OrderedDict[str, SessionRecord] = OrderedDict()
        self._revoked: set[str] = set()
        self._lock = threading.RLock()

    async def load(self, token_hash: str) -> SessionRecord | None:
        with self._lock:
            if token_hash in self._revoked:
                return None
            record = self._records.get(token_hash)
            return _copy_record(record) if record is not None else None

    async def save(self, token_hash: str, record: SessionRecord) -> None:
        with self._lock:
            now = utc_now()
            expired = [
                key for key, value in self._records.items() if value.expires_at <= now
            ]
            for key in expired:
                del self._records[key]
                self._revoked.discard(key)
            for key, value in self._records.items():
                if value.idle_expires_at <= now:
                    self._revoked.add(key)
            if token_hash in self._revoked:
                raise SessionUnavailable("a revoked session cannot be restored")
            if (
                token_hash not in self._records
                and len(self._records) >= self.max_sessions
            ):
                raise SessionCapacityExceeded
            self._records[token_hash] = _copy_record(record)
            self._records.move_to_end(token_hash)

    async def delete(self, token_hash: str) -> None:
        with self._lock:
            if token_hash in self._records:
                self._revoked.add(token_hash)

    async def delete_by_subject(self, subject: str) -> int:
        with self._lock:
            keys = [k for k, v in self._records.items() if v.subject == subject]
            self._revoked.update(keys)
            return len(keys)

    async def purge_expired(self, now: datetime | None = None) -> int:
        instant = now or utc_now()
        with self._lock:
            keys = [
                key
                for key, record in self._records.items()
                if record.expires_at <= instant
            ]
            for key in keys:
                del self._records[key]
                self._revoked.discard(key)
            for key, record in self._records.items():
                if record.idle_expires_at <= instant:
                    self._revoked.add(key)
            return len(keys)


class DatabaseSessionStore(SessionStore):
    """Shared store using an explicitly migrated ``hinoki_sessions`` table."""

    def __init__(self, database: Database | None = None) -> None:
        self._database = database
        self._configured_database = database is not None

    @property
    def database(self) -> Database:
        if self._database is None:
            raise SessionUnavailable("DatabaseSessionStore has no Database")
        return self._database

    def bind(self, database: Database) -> None:
        if not self._configured_database:
            self._database = database

    def unbind(self) -> None:
        if not self._configured_database:
            self._database = None

    async def load(self, token_hash: str) -> SessionRecord | None:
        row = await _session_db(
            self.database.fetch_one(
                "SELECT data, created_at, expires_at, idle_expires_at, last_seen_at, "
                "subject "
                "FROM hinoki_sessions WHERE token_hash = ? AND revoked = 0",
                token_hash,
            )
        )
        if row is None:
            return None
        try:
            data = json.loads(row["data"])
            created = _datetime(row["created_at"])
            expires = _datetime(row["expires_at"])
            idle_expires = _datetime(row["idle_expires_at"])
            last_seen = _datetime(row["last_seen_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SessionUnavailable("stored session data is invalid") from None
        if not isinstance(data, dict):
            raise SessionUnavailable("stored session data is invalid")
        return SessionRecord(
            data, created, expires, idle_expires, last_seen, row["subject"]
        )

    async def save(self, token_hash: str, record: SessionRecord) -> None:
        payload = json.dumps(record.data, separators=(",", ":"), sort_keys=True)
        values = (
            token_hash,
            payload,
            record.created_at.isoformat(),
            record.expires_at.isoformat(),
            record.idle_expires_at.isoformat(),
            record.last_seen_at.isoformat(),
            record.subject,
        )
        sql = _session_upsert(self.database.dialect)
        database = self.database
        affected = await _session_db(database.execute(sql, *values))
        if affected == 0:
            current = await _session_db(
                database.fetch_one(
                    "SELECT revoked FROM hinoki_sessions WHERE token_hash = ?",
                    token_hash,
                )
            )
            if current is None or current["revoked"] != 0:
                raise SessionUnavailable("a revoked session cannot be restored")

    async def delete(self, token_hash: str) -> None:
        await _session_db(
            self.database.execute(
                "UPDATE hinoki_sessions SET revoked = 1 WHERE token_hash = ?",
                token_hash,
            )
        )

    async def delete_by_subject(self, subject: str) -> int:
        return await _session_db(
            self.database.execute(
                "UPDATE hinoki_sessions SET revoked = 1 "
                "WHERE subject = ? AND revoked = 0",
                subject,
            )
        )

    async def purge_expired(self, now: datetime | None = None) -> int:
        instant = (now or utc_now()).isoformat()
        return await _session_db(
            self.database.execute(
                "DELETE FROM hinoki_sessions WHERE expires_at <= ?",
                instant,
            )
        )


async def _session_db[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except Exception as exc:
        log.warning("session database operation failed: %s", type(exc).__name__)
        raise SessionUnavailable from None


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError("invalid timestamp")


def _session_upsert(dialect: str) -> str:
    if dialect in {"mysql", "mariadb"}:
        return MYSQL_SESSION_UPSERT
    return SQLITE_SESSION_UPSERT


class Session:
    """Mutable state scoped to one request and persisted only after success."""

    __hinoki_session__ = True

    def __init__(
        self,
        store: SessionStore,
        settings: SessionSettings,
        token: str | None = None,
        record: SessionRecord | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.store = store
        self.token = token
        self.record = record
        self._data = deepcopy(record.data) if record else {}
        self._original_token_hash = _digest(token) if token is not None else None
        self.created_at = record.created_at if record else (now or utc_now())
        self.expires_at = (
            record.expires_at if record else self.created_at + settings.absolute_timeout
        )
        self.idle_timeout = settings.idle_timeout
        self.idle_expires_at = (
            record.idle_expires_at
            if record
            else self.created_at + settings.idle_timeout
        )
        self.last_seen_at = record.last_seen_at if record else self.created_at
        self._subject = record.subject if record else None
        self.max_bytes = settings.max_bytes
        self.cookie_name = settings.cookie_name
        self.cookie_secure = settings.cookie_secure
        self.csrf_secret = settings.csrf_secret
        self.modified = False
        self.invalidated = False
        self.rotated = False
        self._pending_delete: str | None = None
        self.touch_seconds = settings.touch_seconds
        self.touched = False

    @classmethod
    async def from_cookie(
        cls,
        store: SessionStore,
        value: str | None,
        settings: SessionSettings,
        *,
        now: datetime | None = None,
    ) -> Session:
        """Load a valid, unexpired cookie; invalid tokens start anonymous."""
        now = now or utc_now()
        token = value if value and len(value) <= MAX_COOKIE_TOKEN_BYTES else None
        token_hash = _digest(token) if token is not None else None
        record = await store.load(token_hash) if token_hash is not None else None
        if (
            record is not None
            and token_hash is not None
            and min(record.expires_at, record.idle_expires_at) <= now
        ):
            await store.delete(token_hash)
            record = None
        session = cls(
            store, settings, token if record is not None else None, record, now=now
        )
        if record is not None:
            session.touched = (
                now - record.last_seen_at
            ).total_seconds() >= settings.touch_seconds
        return session

    @property
    def is_new(self) -> bool:
        return self.record is None

    @property
    def subject(self) -> str | None:
        return self._subject

    @subject.setter
    def subject(self, value: str | None) -> None:
        if value is not None and len(value) > MAX_SUBJECT_BYTES:
            raise ValueError("session subject is limited to 255 characters")
        self._subject = value
        self.modified = True

    def get(self, key: str, default: T | None = None) -> object | T | None:
        return deepcopy(self._data.get(key, default))

    @property
    def data(self) -> Mapping[str, object]:
        """Read-only state view for authentication resolvers."""
        return MappingProxyType(deepcopy(self._data))

    def __getitem__(self, key: str) -> object:
        return deepcopy(self._data[key])

    def __setitem__(self, key: str, value: object) -> None:
        candidate = {**self._data, key: deepcopy(value)}
        self._check_size(candidate)
        self._data = candidate
        self.modified = True

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self.modified = True

    def clear(self) -> None:
        self._data.clear()
        self.modified = True

    async def rotate(self) -> None:
        self._record_original_token_for_revocation()
        self.token = secrets.token_urlsafe(32)
        self.rotated = True
        self.modified = True

    async def invalidate(self) -> None:
        self._record_original_token_for_revocation()
        self._data.clear()
        self.invalidated = True
        self.modified = True

    def csrf_token(self) -> str:
        if self.token is None:
            raise SessionError("session has no token")
        return hmac.new(self.csrf_secret, self.token.encode(), "sha256").hexdigest()

    def verify_csrf(self, token: str) -> bool:
        return hmac.compare_digest(self.csrf_token(), token)

    def _record_original_token_for_revocation(self) -> None:
        if self._pending_delete is None:
            self._pending_delete = self._original_token_hash

    def _check_size(self, data: dict[str, object] | None = None) -> None:
        try:
            checked = self._data if data is None else data
            size = len(json.dumps(checked, separators=(",", ":")).encode())
        except (TypeError, ValueError):
            raise TypeError("session values must be JSON serializable") from None
        if size > self.max_bytes:
            raise SessionTooLarge

    async def commit(self, now: datetime | None = None) -> tuple[str, str] | None:
        if not self.modified and not self.touched:
            return None
        if self.invalidated:
            if self._pending_delete is not None:
                await self.store.delete(self._pending_delete)
            return set_cookie(
                self.cookie_name, "", max_age=0, secure=self.cookie_secure
            )
        if self.token is None:
            self.token = secrets.token_urlsafe(32)
        instant = now or utc_now()
        if self.expires_at <= instant:
            raise SessionUnavailable("the session has expired")
        self._check_size()
        await self.store.save(
            _digest(self.token),
            SessionRecord(
                self._data,
                self.created_at,
                self.expires_at,
                min(self.expires_at, instant + self.idle_timeout),
                instant,
                self.subject,
            ),
        )
        if self._pending_delete is not None:
            await self.store.delete(self._pending_delete)
        max_age = max(
            0,
            min(
                int(self.idle_timeout.total_seconds()),
                int((self.expires_at - instant).total_seconds()),
            ),
        )
        return set_cookie(
            self.cookie_name,
            self.token,
            max_age=max_age,
            secure=self.cookie_secure,
        )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionModule(Module):
    """Provides a worker-owned session store and session policy."""

    name = "sessions"

    def __init__(
        self, store: SessionStore, settings: SessionSettings | None = None
    ) -> None:
        self.store = store
        self.settings = settings or SessionSettings()
        requirements = (
            (DatabaseModule,)
            if isinstance(store, DatabaseSessionStore) and store._database is None
            else ()
        )
        object.__setattr__(self, "requires", requirements)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        database_store = (
            self.store if isinstance(self.store, DatabaseSessionStore) else None
        )
        bound = database_store is not None and not database_store._configured_database
        if database_store is not None and bound:
            database_store.bind(ctx.get(Database))
        try:
            ctx.provide(SessionStore, self.store)
            ctx.provide(SessionSettings, self.settings)
            yield
        finally:
            if bound and database_store is not None:
                database_store.unbind()


def migrations_path() -> Path:
    """Path to the explicit application-session migration files."""
    return Path(__file__).parent / "session_migrations"


class SessionAuth(BaseAuth[T]):
    """Resolve an authenticated caller from the active request's session data."""

    challenge = None
    __hinoki_session_auth__ = True

    def __init__(
        self,
        resolve: Callable[
            [Mapping[str, object]],
            Caller[T] | Awaitable[Caller[T] | None] | None,
        ],
        *,
        cookie_name: str = "hinoki_session",
    ):
        self.resolve = resolve
        self.cookie_name = cookie_name

    def openapi_scheme(self) -> dict[str, object]:
        return {"type": "apiKey", "in": "cookie", "name": self.cookie_name}

    async def authenticate(self, credentials: Credentials) -> Caller[T] | None:
        session = credentials.session
        if not isinstance(session, Session):
            raise Unauthorized()
        caller = cast(Caller[T] | None, await call_hook(self.resolve, session.data))
        if caller is None:
            raise Unauthorized()
        return caller


__all__ = [
    "DatabaseSessionStore",
    "MemorySessionStore",
    "Session",
    "SessionAuth",
    "SessionCapacityExceeded",
    "SessionError",
    "SessionModule",
    "SessionRecord",
    "SessionSettings",
    "SessionStore",
    "SessionTooLarge",
    "SessionUnavailable",
    "migrations_path",
]
