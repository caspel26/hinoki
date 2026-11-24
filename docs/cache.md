---
status: implemented
---

# Cache values

`Cache` stores bounded byte values or JSON-compatible values with an explicit
finite TTL. Install a backend module and request `Cache` in the handler:

```python
from datetime import timedelta

from hinoki import Hinoki, Schema, http
from hinoki.cache import Cache, CacheModule, MemoryCache


class Summary(Schema):
    total: int


@http.get("/summary")
async def summary(cache: Cache) -> Summary:
    cached = await cache.get_schema("summary", Summary)
    if cached is not None:
        return cached
    fresh = Summary(total=await calculate_total())
    await cache.set_schema("summary", fresh, ttl=timedelta(seconds=60))
    return fresh


app = Hinoki(
    include=[summary],
    modules=[CacheModule(MemoryCache(max_entries=5_000, max_bytes=32 * 1024 * 1024))],
)
```

The API includes async `get`, `set`, `delete`, `get_bytes`, `set_bytes`,
`get_schema`, `set_schema` and `get_or_set`, plus `get_sync`, `set_sync`,
`delete_sync`, `get_bytes_sync` and `set_bytes_sync`. A sync handler can use the
sync methods. The memory backend calls its Rust cache directly; the database
backend bridges its sync methods to the worker event loop, so use those only in
a Hinoki sync handler or another Hinoki sync execution context.

`get` returns `None` for a miss or expired value. `None` cannot be stored as a
JSON value because that would make a stored null indistinguishable from a miss;
use a tagged object if the application needs that distinction. `set` always
requires a TTL between a positive value and 366 days. Keys are UTF-8 strings of
1–256 bytes without NUL. `cache.namespace("users")` returns a view that prefixes
keys and enforces the same full-key limit. The view delegates to its cache, so a
namespace taken from a `DatabaseCache` follows that cache's worker binding and
cleanup.

`set_schema` stores the schema's declared fields only: `@computed_field` values
are omitted and recomputed when `get_schema` validates the value again, so
schemas with the default `extra="forbid"` round-trip. A schema containing a
`Secret` field, including in a nested schema, is rejected with `ValueError`
instead of caching a masked or plain secret.

`get_or_set` reads, calls its factory on a miss and writes the result. It does
not coalesce concurrent misses, so applications with expensive factories may
need their own single-flight or locking policy. Schema validation mismatches in
`get_schema` delete the stale value and return a miss; malformed JSON raises
`CacheDecodeError`.

## Backends and ownership

| Backend | State and visibility | Limits and expiration |
| --- | --- | --- |
| `MemoryCache(max_entries=10_000, max_bytes=64 MiB)` | Rust `hinoki.core.MemoryCache`, owned by one worker process; not shared by other workers | Both entry count and encoded bytes are bounded; each value defaults to at most 1 MiB; monotonic TTL and clock-style eviction |
| `DatabaseCache()` | Uses the worker's `Database`; visible across processes sharing that database | Default value limit 1 MiB; TTL is checked on read; call `purge_expired()` from scheduled maintenance |

The native memory cache is guarded by a Rust mutex, copies bytes at the Python
binding, and uses a second-chance clock for eviction. Its state is process-local;
multiple workers do not share or invalidate each other's entries. The database
backend is shared through the selected database, but has higher I/O cost and no
configured global table-size quota. Values are stored as hex text, so database
storage uses more bytes than the original payload. No periodic cleanup job is
started by Hinoki.

`CacheModule(MemoryCache(...))` needs no database. `CacheModule(DatabaseCache())`
requires the application's `DatabaseModule` and uses that worker's connection
pool. The database backend needs the `hinoki_cache` table. Apply the SQL at
`migrations_path() / "0001-initial.sql"` through an explicit reviewed project
migration; neither import nor lifespan creates a table.

## Failures

Cache failures are visible by default. `CacheUnavailable` is 503,
`InvalidCacheKey` is 400, `CacheValueTooLarge` is 413 and `CacheDecodeError` is
500. Invalid TTLs and non-JSON values raise `ValueError` or `TypeError` before
the backend is called. Backend database errors become `CacheUnavailable`; a
route that wants a best-effort fallback should catch `CacheError` explicitly.

Cache injection does not add a `Request` parameter and is excluded from the
OpenAPI document and MCP input schema. A route that uses a cache still follows
the normal Python handler boundary when its cache calls are ordinary Python
awaitables; the Rust memory backend only moves byte storage, expiry and
eviction into Rust. The cache does not implement HTTP response caching, ORM
query caching, distributed invalidation, or security-sensitive rate limiting.
