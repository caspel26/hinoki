# Store uploaded files

`FileStorage` stores bounded byte values behind opaque keys. Install a
`FileStorageModule` and request `FileStorage` in a handler:

```python
from pathlib import Path

from hinoki import Hinoki, Schema, UploadedFile, http
from hinoki.http.response import Response
from hinoki.storage import FileStorage, FileStorageModule, LocalFileStorage


class UploadResult(Schema):
    key: str
    size: int
    sha256: str


@http.post("/documents", status=201)
async def upload(document: UploadedFile, storage: FileStorage) -> UploadResult:
    saved = await storage.save(document)
    return UploadResult(key=saved.key, size=saved.size, sha256=saved.sha256)


@http.get("/documents")
async def download(key: str, storage: FileStorage) -> Response:
    return await storage.open_response(key)


app = Hinoki(
    include=[upload, download],
    modules=[FileStorageModule(LocalFileStorage(Path("var/uploads")))],
)
```

`FileStorage` can also be used from a synchronous `def` handler with
`save_sync`, `read_sync`, `exists_sync` and `delete_sync`. The async API is
`save`, `read`, `exists`, `delete` and `open_response`. `save` accepts an
`UploadedFile` or `bytes` and returns `StoredFile(key, size, content_type,
sha256, filename)`. Empty files are allowed.

Pass the opaque key as the `key` query parameter. Generated keys contain `/`
for filesystem sharding, so they do not fit a single `{key}` path segment.

## Choose a backend

| Backend | Ownership and visibility | Bounds and I/O |
| --- | --- | --- |
| `MemoryFileStorage` | Owned by the worker process; workers do not share values | Per-file and total byte limits; async methods perform in-memory work |
| `LocalFileStorage(root)` | Files on the configured filesystem; workers on one host can share a volume; separate hosts need a shared volume | Per-file limit and a local quota counter; async filesystem operations use a bounded per-worker thread pool |

Both backends default to a 10 MiB per-file limit. `MemoryFileStorage` defaults
to a 64 MiB total limit. `LocalFileStorage(max_total_bytes=...)` checks the
filesystem size when the worker starts and tracks its own writes and deletes.
That counter is per worker process; concurrent workers can each accept a write
before seeing the other's counter, so it is not a global quota. No background
cleanup or cross-host coordination is provided.

`open_response()` returns a `Response` for either backend. The memory backend
buffers its bounded value in that response; the local backend returns a streamed
`File` (a `Response` subtype), sent by the server without copying its body into
a Python response buffer.

Pass `filename=` and `content_type=` to `open_response()` to set the download
name and media type. Without them both backends behave alike: the stored
content type is used unless it is `application/octet-stream`, otherwise the type
is guessed from the file name, and a file with no name is sent as a plain
`attachment`. The storage key is never offered as a file name.

`disposition="inline"` is honored only for passive media types: `image/png`,
`image/jpeg`, `image/gif`, `image/webp`, `image/avif`, `application/pdf` and
`text/plain`. Any other type, such as `text/html`, `image/svg+xml` or
`text/javascript`, is sent as `attachment` so an uploaded file cannot run script
in the application's origin. An unknown disposition raises `ValueError`.

`LocalFileStorage` creates its root during worker startup, checks that it is a
writable directory, uses unique generated keys and atomically replaces a
temporary file. Directories use mode `0700` and files use `0600`. The uploaded
filename is metadata and is never used as a filesystem path.

## Errors and access control

The public errors are `FileTooLarge` (413), `StorageQuotaExceeded` (507),
`InvalidStorageKey` (400), `StoredFileNotFound` (404) and
`StorageUnavailable` (503). A malformed key or filesystem failure does not
include a raw path in its public message. A missing `FileStorageModule` fails
startup before a route that requests `FileStorage` becomes available.

Storage checks whether a key is valid; it does not decide who owns that key.
Routes must apply their own `Access` policy and verify the record's tenant or
user ownership before reading or deleting a key. A successful database
transaction does not make the filesystem write transactional: an application
that saves a blob and later rolls back must delete or reconcile that blob
itself. Uploaded parts are first parsed into bounded in-memory `UploadedFile`
values; storage does not stream request uploads directly to disk.
