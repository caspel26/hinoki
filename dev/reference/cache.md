# Cache

`from hinoki.cache import Cache, CacheModule, MemoryCache, DatabaseCache`. The [cache guide](https://hinoki.caspel.dev/dev/cache.md) explains backends, TTLs and migrations.

- `hinoki.cache.Cache() -> 'None'`: Base resource API with async methods and explicit sync counterparts. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.Cache)

- `hinoki.cache.CacheModule(cache: 'Cache') -> 'None'`: Provides a worker-owned cache backend to annotated route handlers. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.CacheModule)

- `hinoki.cache.MemoryCache(max_entries: 'int' = 10000, max_bytes: 'int' = 67108864, max_value_bytes: 'int | None' = None) -> 'None'`: Rust-backed bounded cache; state belongs to this worker process. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.MemoryCache)

- `hinoki.cache.DatabaseCache(database: 'Database | None' = None, *, max_value_bytes: 'int' = 1048576) -> 'None'`: Shared DB cache using an explicit ``hinoki_cache`` migration. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.DatabaseCache)

- `hinoki.cache.CacheError(...)`: Base class for explicit cache failures. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.CacheError)

- `hinoki.cache.CacheUnavailable(...)`: The selected cache backend failed. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.CacheUnavailable)

- `hinoki.cache.InvalidCacheKey(...)`: A cache key is empty, too long, or contains a forbidden byte. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.InvalidCacheKey)

- `hinoki.cache.CacheValueTooLarge(...)`: The encoded cache value exceeds its configured bound. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.CacheValueTooLarge)

- `hinoki.cache.CacheDecodeError(...)`: A stored value is not valid JSON. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.CacheDecodeError)

- `hinoki.cache.migrations_path() -> 'Path'`: Path to the explicit DB cache migration file. [reference](https://hinoki.caspel.dev/dev/reference/cache/#hinoki.cache.migrations_path)
