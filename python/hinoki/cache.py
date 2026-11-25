"""Typed JSON and byte caches with bounded memory and explicit DB storage."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from hinoki import core
from hinoki.blocking import run_sync
from hinoki.db import Database, DatabaseModule
from hinoki.errors import APIError, ValidationError
from hinoki.modules import Module, ModuleContext, Resource
from hinoki.schema import Schema
from hinoki.utils import utc_now

if TYPE_CHECKING:
    from hinoki.core import Row
    from hinoki.fieldtypes import FieldType

S = TypeVar("S", bound=Schema)
log = logging.getLogger("hinoki.cache")
MAX_KEY_BYTES = 256
DEFAULT_MAX_VALUE_BYTES = 1024 * 1024
MAX_TTL_SECONDS = 366 * 24 * 60 * 60
MYSQL_UPSERT = (
    "INSERT INTO hinoki_cache (cache_key, value_hex, expires_at) VALUES (?, ?, ?) "
    "ON DUPLICATE KEY UPDATE value_hex = VALUES(value_hex), "
    "expires_at = VALUES(expires_at)"
)
SQLITE_UPSERT = (
    "INSERT INTO hinoki_cache (cache_key, value_hex, expires_at) VALUES (?, ?, ?) "
    "ON CONFLICT(cache_key) DO UPDATE SET value_hex = excluded.value_hex, "
    "expires_at = excluded.expires_at"
)


class CacheError(APIError):
    """Base class for explicit cache failures."""

    status = 500
    code = "cache_error"
    message = "Cache operation failed."


class CacheUnavailable(CacheError):
    """The selected cache backend failed."""

    status = 503
    code = "cache_unavailable"
    message = "Cache storage is unavailable."


class InvalidCacheKey(CacheError):
    """A cache key is empty, too long, or contains a forbidden byte."""

    status = 400
    code = "invalid_cache_key"
    message = "The cache key is invalid."


class CacheValueTooLarge(CacheError):
    """The encoded cache value exceeds its configured bound."""

    status = 413
    code = "cache_value_too_large"
    message = "The cache value exceeds its size limit."


class CacheDecodeError(CacheError):
    """A stored value is not valid JSON."""

    status = 500
    code = "cache_decode_error"
    message = "A cached value could not be decoded."


def _ttl_seconds(value: float | timedelta) -> float:
    return core.validate_cache_ttl(value)


def _positive_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _json_bytes(value: object) -> bytes:
    if value is None:
        raise TypeError("None is reserved to represent a cache miss")
    try:
        return core.encode_json(value)
    except (TypeError, ValueError):
        raise TypeError("cache values must be JSON serializable") from None


def _decode_json(raw: bytes) -> object:
    try:
        return core.decode_json(raw)
    except (ValueError, TypeError):
        raise CacheDecodeError from None


def _key(value: str, prefix: str = "") -> str:
    try:
        return core.validate_cache_key(value, prefix)
    except ValueError:
        raise InvalidCacheKey from None


class Cache(Resource):
    """Base resource API with async methods and explicit sync counterparts."""

    def __init__(self) -> None:
        self._prefix = ""

    def namespace(self, name: str) -> Cache:
        """A view whose keys are prefixed with ``name``; it shares this backend,
        so the backend's worker binding and cleanup still apply to it."""
        return _NamespacedCache(self, _key(name))

    def _name(self, key: str) -> str:
        return _key(key, self._prefix)

    async def get_bytes(self, key: str) -> bytes | None:
        raise NotImplementedError

    def get_bytes_sync(self, key: str) -> bytes | None:
        raise NotImplementedError

    async def set_bytes(
        self, key: str, value: bytes, *, ttl: float | timedelta
    ) -> None:
        raise NotImplementedError

    def set_bytes_sync(self, key: str, value: bytes, *, ttl: float | timedelta) -> None:
        raise NotImplementedError

    async def delete(self, key: str) -> bool:
        raise NotImplementedError

    def delete_sync(self, key: str) -> bool:
        raise NotImplementedError

    async def get(self, key: str) -> object | None:
        raw = await self.get_bytes(key)
        return None if raw is None else _decode_json(raw)

    def get_sync(self, key: str) -> object | None:
        raw = self.get_bytes_sync(key)
        return None if raw is None else _decode_json(raw)

    async def set(self, key: str, value: object, *, ttl: float | timedelta) -> None:
        await self.set_bytes(key, _json_bytes(value), ttl=ttl)

    def set_sync(self, key: str, value: object, *, ttl: float | timedelta) -> None:
        self.set_bytes_sync(key, _json_bytes(value), ttl=ttl)

    async def get_or_set[T](
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        ttl: float | timedelta,
    ) -> T:
        found = await self.get(key)
        if found is not None:
            return cast(T, found)
        value = await factory()
        await self.set(key, value, ttl=ttl)
        return value

    async def get_schema(self, key: str, schema: type[S]) -> S | None:
        value = await self.get(key)
        if value is None:
            return None
        try:
            return schema.validate(value)
        except ValidationError:
            await self.delete(key)
            return None

    async def set_schema(self, key: str, value: S, *, ttl: float | timedelta) -> None:
        """Store the declared fields of ``value``; computed values are left out
        so ``get_schema`` validates them back. A schema with a ``Secret``
        field is refused: the cache would only ever hold its mask."""
        await self.set_bytes(key, _schema_bytes(value), ttl=ttl)


def _schema_bytes(value: Schema) -> bytes:
    if not isinstance(value, Schema):
        raise TypeError("set_schema stores a Schema instance")
    secret = _secret_field(type(value))
    if secret is not None:
        raise ValueError(
            f"{secret} is a Secret field; secrets cannot be cached because "
            "they serialize masked"
        )
    return core.encode_json(core.dump_for_storage(value))


def _secret_field(
    schema: type[Schema], seen: frozenset[type] = frozenset()
) -> str | None:
    """``Schema.field`` path of the first ``Secret`` declared in ``schema``."""
    if schema in seen:
        return None
    for name, spec in schema.specs().items():
        found = _secret_in(spec.ftype, seen | {schema})
        if found is not None:
            return f"{schema.__name__}.{name}" if found == "" else found
    return None


def _secret_in(ftype: FieldType, seen: frozenset[type]) -> str | None:
    """``""`` for a secret here, a nested path, or ``None`` when there is none."""
    if ftype.kind == "secret":
        return ""
    nested = [ftype.schema, *(variant for _, variant in ftype.variants)]
    for schema in nested:
        if schema is not None:
            found = _secret_field(schema, seen)
            if found is not None:
                return found
    for child in ftype.children():
        found = _secret_in(child, seen)
        if found is not None:
            return found
    return None


class _NamespacedCache(Cache):
    """A key-prefixed view that delegates every operation to its backend."""

    def __init__(self, backend: Cache, prefix: str) -> None:
        super().__init__()
        self._backend = backend
        self._prefix = prefix

    def namespace(self, name: str) -> Cache:
        return _NamespacedCache(self._backend, _key(name, self._prefix))

    def __getattr__(self, name: str) -> object:
        # Backend-specific helpers such as ``purge_expired`` or ``database``.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._backend, name)

    async def get_bytes(self, key: str) -> bytes | None:
        return await self._backend.get_bytes(self._name(key))

    def get_bytes_sync(self, key: str) -> bytes | None:
        return self._backend.get_bytes_sync(self._name(key))

    async def set_bytes(
        self, key: str, value: bytes, *, ttl: float | timedelta
    ) -> None:
        await self._backend.set_bytes(self._name(key), value, ttl=ttl)

    def set_bytes_sync(self, key: str, value: bytes, *, ttl: float | timedelta) -> None:
        self._backend.set_bytes_sync(self._name(key), value, ttl=ttl)

    async def delete(self, key: str) -> bool:
        return await self._backend.delete(self._name(key))

    def delete_sync(self, key: str) -> bool:
        return self._backend.delete_sync(self._name(key))


class MemoryCache(Cache):
    """Rust-backed bounded cache; state belongs to this worker process."""

    def __init__(
        self,
        max_entries: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        max_value_bytes: int | None = None,
    ) -> None:
        super().__init__()
        _positive_limit(max_entries, "max_entries")
        _positive_limit(max_bytes, "max_bytes")
        value_limit = min(DEFAULT_MAX_VALUE_BYTES, max_bytes)
        if max_value_bytes is not None:
            _positive_limit(max_value_bytes, "max_value_bytes")
            value_limit = max_value_bytes
        if value_limit < 1 or value_limit > max_bytes:
            raise ValueError("max_value_bytes must be within max_bytes")
        self.max_value_bytes = value_limit
        self._native = core.MemoryCache(max_entries, max_bytes)

    async def get_bytes(self, key: str) -> bytes | None:
        return self.get_bytes_sync(key)

    def get_bytes_sync(self, key: str) -> bytes | None:
        return self._native.get(self._name(key))

    async def set_bytes(
        self, key: str, value: bytes, *, ttl: float | timedelta
    ) -> None:
        self.set_bytes_sync(key, value, ttl=ttl)

    def set_bytes_sync(self, key: str, value: bytes, *, ttl: float | timedelta) -> None:
        if len(value) > self.max_value_bytes:
            raise CacheValueTooLarge
        self._native.set(self._name(key), bytes(value), _ttl_seconds(ttl))

    async def delete(self, key: str) -> bool:
        return self.delete_sync(key)

    def delete_sync(self, key: str) -> bool:
        return self._native.delete(self._name(key))


class DatabaseCache(Cache):
    """Shared DB cache using an explicit ``hinoki_cache`` migration."""

    def __init__(
        self,
        database: Database | None = None,
        *,
        max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
    ) -> None:
        super().__init__()
        _positive_limit(max_value_bytes, "max_value_bytes")
        self._database = database
        self._configured_database = database is not None
        self.max_value_bytes = max_value_bytes

    @property
    def database(self) -> Database:
        if self._database is None:
            raise CacheUnavailable("DatabaseCache has no Database")
        return self._database

    def bind(self, database: Database) -> None:
        if not self._configured_database:
            self._database = database

    def unbind(self) -> None:
        if not self._configured_database:
            self._database = None

    async def get_bytes(self, key: str) -> bytes | None:
        full_key = self._name(key)
        row = await _cache_db(
            self.database.fetch_one(
                "SELECT value_hex, expires_at FROM hinoki_cache WHERE cache_key = ?",
                full_key,
            )
        )
        if row is None:
            return None
        expires = _datetime(row["expires_at"])
        if expires <= utc_now():
            await self._delete_observed(full_key, row)
            return None
        try:
            value = bytes.fromhex(row["value_hex"])
        except (TypeError, ValueError):
            raise CacheDecodeError("stored bytes are invalid") from None
        if len(value) > self.max_value_bytes:
            raise CacheValueTooLarge
        return value

    async def _delete_observed(self, key: str, row: Row) -> None:
        await _cache_db(
            self.database.execute(
                "DELETE FROM hinoki_cache WHERE cache_key = ? "
                "AND value_hex = ? AND expires_at = ?",
                key,
                row["value_hex"],
                row["expires_at"],
            )
        )

    def get_bytes_sync(self, key: str) -> bytes | None:
        return run_sync(self.get_bytes(key))

    async def set_bytes(
        self, key: str, value: bytes, *, ttl: float | timedelta
    ) -> None:
        full_key = self._name(key)
        if len(value) > self.max_value_bytes:
            raise CacheValueTooLarge
        expiry = (utc_now() + timedelta(seconds=_ttl_seconds(ttl))).isoformat()
        sql = (
            MYSQL_UPSERT
            if self.database.dialect in {"mysql", "mariadb"}
            else SQLITE_UPSERT
        )
        await _cache_db(
            self.database.execute(sql, full_key, bytes(value).hex(), expiry)
        )

    def set_bytes_sync(self, key: str, value: bytes, *, ttl: float | timedelta) -> None:
        run_sync(self.set_bytes(key, value, ttl=ttl))

    async def delete(self, key: str) -> bool:
        return bool(
            await _cache_db(
                self.database.execute(
                    "DELETE FROM hinoki_cache WHERE cache_key = ?", self._name(key)
                )
            )
        )

    def delete_sync(self, key: str) -> bool:
        return run_sync(self.delete(key))

    async def purge_expired(self, now: datetime | None = None) -> int:
        return await _cache_db(
            self.database.execute(
                "DELETE FROM hinoki_cache WHERE expires_at <= ?",
                (now or utc_now()).isoformat(),
            )
        )


async def _cache_db[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except Exception as exc:
        log.warning("cache database operation failed: %s", type(exc).__name__)
        raise CacheUnavailable from None


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise CacheDecodeError("stored expiry is invalid") from None
    raise CacheDecodeError("stored expiry is invalid")


class CacheModule(Module):
    """Provides a worker-owned cache backend to annotated route handlers."""

    name = "cache"

    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        requirements = (
            (DatabaseModule,)
            if isinstance(cache, DatabaseCache) and cache._database is None
            else ()
        )
        object.__setattr__(self, "requires", requirements)

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        bound = isinstance(self.cache, DatabaseCache) and not (
            self.cache._configured_database
        )
        if bound and isinstance(self.cache, DatabaseCache):
            self.cache.bind(ctx.get(Database))
        try:
            ctx.provide(Cache, self.cache)
            if type(self.cache) is not Cache:
                ctx.provide(type(self.cache), self.cache)
            yield
        finally:
            if bound and isinstance(self.cache, DatabaseCache):
                self.cache.unbind()


def migrations_path() -> Path:
    """Path to the explicit DB cache migration file."""
    return Path(__file__).parent / "cache_migrations"


__all__ = [
    "Cache",
    "CacheDecodeError",
    "CacheError",
    "CacheModule",
    "CacheUnavailable",
    "CacheValueTooLarge",
    "DatabaseCache",
    "InvalidCacheKey",
    "MemoryCache",
    "migrations_path",
]
