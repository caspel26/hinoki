"""Native memory and shared database cache behavior."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.cache import (
    Cache,
    CacheDecodeError,
    CacheModule,
    CacheValueTooLarge,
    DatabaseCache,
    InvalidCacheKey,
    MemoryCache,
)
from hinoki.db import Database
from hinoki.runtime.servercore import native_handler_eligible
from hinoki.testing import ASGIClient

if TYPE_CHECKING:
    from hinoki.core import Param, Row


class Summary(Schema):
    total: int


@http.get("/cache/async")
async def async_cached(cache: Cache) -> dict[str, object]:
    value = await cache.get("value")
    return {"value": value}


@http.get("/cache/sync")
def sync_cached(cache: Cache) -> dict[str, object]:
    return {"value": cache.get_sync("value")}


async def test_memory_cache_typed_bytes_schema_and_namespaces() -> None:
    cache = MemoryCache(max_entries=4, max_bytes=1024)
    await cache.set("value", {"count": 3}, ttl=timedelta(seconds=30))
    assert await cache.get("value") == {"count": 3}
    namespaced = cache.namespace("users")
    await namespaced.set("value", "separate", ttl=30)
    assert await namespaced.get("value") == "separate"
    assert await cache.get("value") == {"count": 3}
    await cache.set_schema("summary", Summary(total=7), ttl=30)
    assert await cache.get_schema("summary", Summary) == Summary(total=7)
    await cache.set_bytes("raw", b"", ttl=30)
    assert await cache.get_bytes("raw") == b""
    assert await cache.delete("raw")
    assert not await cache.delete("raw")


async def test_cache_validates_limits_ttl_keys_and_decode() -> None:
    cache = MemoryCache(max_entries=2, max_bytes=16, max_value_bytes=8)
    with pytest.raises(CacheValueTooLarge):
        await cache.set_bytes("large", b"123456789", ttl=2)
    with pytest.raises(InvalidCacheKey):
        await cache.set("", "x", ttl=2)
    with pytest.raises(ValueError, match="ttl"):
        await cache.set("bad-ttl", "x", ttl=float("nan"))
    with pytest.raises(TypeError, match="None is reserved"):
        await cache.set("none", None, ttl=2)
    await cache.set_bytes("corrupt", b"not-json", ttl=2)
    with pytest.raises(CacheDecodeError):
        await cache.get("corrupt")


async def test_memory_cache_ttl_and_get_or_set() -> None:
    cache = MemoryCache(max_entries=2, max_bytes=64)
    calls = 0

    async def make_value() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"count": calls}

    assert await cache.get_or_set("lazy", make_value, ttl=30) == {"count": 1}
    assert await cache.get_or_set("lazy", make_value, ttl=30) == {"count": 1}
    await cache.set("short", "expires", ttl=0.001)
    await asyncio.sleep(0.01)
    assert await cache.get("short") is None


async def test_schema_version_mismatch_is_a_cache_miss() -> None:
    class Old(Schema):
        title: str

    class New(Schema):
        total: int

    cache = MemoryCache(max_entries=4, max_bytes=1024)
    await cache.set_schema("record", Old(title="old"), ttl=30)
    assert await cache.get_schema("record", New) is None
    assert await cache.get("record") is None


async def test_memory_cache_injected_into_async_and_sync_handlers() -> None:
    cache = MemoryCache(max_entries=4, max_bytes=1024)
    await cache.set("value", "hit", ttl=30)
    app = Hinoki(include=[async_cached, sync_cached], modules=[CacheModule(cache)])
    async with ASGIClient(app) as client:
        assert (await client.get("/cache/async")).body == {"value": "hit"}
        assert (await client.get("/cache/sync")).body == {"value": "hit"}
    assert native_handler_eligible(app.bindings[0])


async def test_database_cache_is_shared_and_expires(tmp_path: object) -> None:
    database = await Database.connect(f"sqlite://{tmp_path}/cache.sqlite3")
    try:
        await database.execute(
            "CREATE TABLE hinoki_cache (cache_key varchar(256) PRIMARY KEY, "
            "value_hex text NOT NULL, expires_at text NOT NULL)"
        )
        cache = DatabaseCache(database)
        await cache.set("value", {"shared": True}, ttl=20)
        peer = DatabaseCache(database)
        assert await peer.get("value") == {"shared": True}
        assert await cache.delete("value")
        assert await peer.get("value") is None
        await cache.set("expired", "old", ttl=0.001)
        await asyncio.sleep(0.01)
        assert await cache.get("expired") is None
        assert await cache.purge_expired() == 0
    finally:
        database.close()


async def test_expired_read_does_not_delete_concurrent_refresh(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = await Database.connect(f"sqlite://{tmp_path}/cache-race.sqlite3")
    try:
        await database.execute(
            "CREATE TABLE hinoki_cache (cache_key varchar(256) PRIMARY KEY, "
            "value_hex text NOT NULL, expires_at text NOT NULL)"
        )
        cache = DatabaseCache(database)
        await database.execute(
            "INSERT INTO hinoki_cache VALUES (?, ?, ?)",
            "race",
            b"stale".hex(),
            "2000-01-01T00:00:00+00:00",
        )
        original_fetch_one = database.fetch_one

        async def read_then_refresh(sql: str, *params: Param) -> Row | None:
            row = await original_fetch_one(sql, *params)
            if sql.startswith("SELECT value_hex"):
                await cache.set_bytes("race", b"fresh", ttl=30)
            return row

        monkeypatch.setattr(database, "fetch_one", read_then_refresh)
        assert await cache.get_bytes("race") is None
        monkeypatch.undo()
        assert await cache.get_bytes("race") == b"fresh"
    finally:
        database.close()
