# File storage

`from hinoki.storage import FileStorage, FileStorageModule, LocalFileStorage`. The [storage guide](https://hinoki.caspel.dev/dev/storage.md) explains keys, limits and downloads.

- `hinoki.storage.FileStorage()`: Backend contract for storing opaque file keys and bounded byte values. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.FileStorage)

- `hinoki.storage.FileStorageModule(storage: 'FileStorage') -> 'None'`: Starts and closes the configured file backend once per worker. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.FileStorageModule)

- `hinoki.storage.LocalFileStorage(...)`: Atomic local-disk backend; files are shared only through a shared volume. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.LocalFileStorage)

- `hinoki.storage.MemoryFileStorage(*, max_file_bytes: 'int' = 10485760, max_total_bytes: 'int' = 67108864) -> 'None'`: Thread-safe, bounded storage for tests and single-worker development. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.MemoryFileStorage)

- `hinoki.storage.StoredFile(key: 'str', size: 'int', content_type: 'str', sha256: 'str', filename: 'str' = '') -> None`: Metadata returned after writing a blob; ``filename`` is never a path. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.StoredFile)

- `hinoki.storage.StorageError(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.StorageError)

- `hinoki.storage.FileTooLarge(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.FileTooLarge)

- `hinoki.storage.StorageQuotaExceeded(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.StorageQuotaExceeded)

- `hinoki.storage.InvalidStorageKey(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.InvalidStorageKey)

- `hinoki.storage.StoredFileNotFound(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.StoredFileNotFound)

- `hinoki.storage.StorageUnavailable(...)`: Base for failures raised by a storage backend. [reference](https://hinoki.caspel.dev/dev/reference/storage/#hinoki.storage.StorageUnavailable)
