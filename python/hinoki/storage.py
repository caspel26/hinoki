"""Explicit worker-owned file storage for bounded uploads."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import mimetypes
import os
import re
import tempfile
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from hinoki import core
from hinoki.errors import APIError
from hinoki.http.response import Response
from hinoki.http.stream import File
from hinoki.modules import Module, ModuleContext, Resource
from hinoki.parsers import UploadedFile

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MEMORY_MAX_BYTES = 64 * 1024 * 1024
MAX_KEY_BYTES = 128
MAX_CONTENT_TYPE_BYTES = 255
KEY_PATTERN = re.compile(r"[a-z0-9/_-]+\Z")
CONTENT_TYPE_PATTERN = re.compile(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+\Z")
T = TypeVar("T")


class StorageError(APIError):
    """Base for failures raised by a storage backend."""

    status = 500
    code = "storage_error"
    message = "File storage failed."


class FileTooLarge(StorageError):
    status = 413
    code = "file_too_large"
    message = "The file exceeds its size limit."


class StorageQuotaExceeded(StorageError):
    status = 507
    code = "storage_quota_exceeded"
    message = "File storage is full."


class InvalidStorageKey(StorageError):
    status = 400
    code = "invalid_storage_key"
    message = "The storage key is invalid."


class StoredFileNotFound(StorageError):
    status = 404
    code = "stored_file_not_found"
    message = "The stored file was not found."


class StorageUnavailable(StorageError):
    status = 503
    code = "storage_unavailable"
    message = "File storage is unavailable."


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Metadata returned after writing a blob; ``filename`` is never a path."""

    key: str
    size: int
    content_type: str
    sha256: str
    filename: str = ""


class FileStorage(Resource):
    """Backend contract for storing opaque file keys and bounded byte values."""

    async def save(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        raise NotImplementedError

    def save_sync(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        raise NotImplementedError

    async def read(self, key: str, *, max_bytes: int | None = None) -> bytes:
        raise NotImplementedError

    def read_sync(self, key: str, *, max_bytes: int | None = None) -> bytes:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    def exists_sync(self, key: str) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> bool:
        raise NotImplementedError

    def delete_sync(self, key: str) -> bool:
        raise NotImplementedError

    async def open_response(
        self,
        key: str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        disposition: Literal["attachment", "inline"] = "attachment",
    ) -> Response:
        raise NotImplementedError


def _whole(value: int, name: str, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _file_parts(
    file: UploadedFile | bytes,
    content_type: str | None,
    filename: str | None,
) -> tuple[bytes, str, str]:
    if isinstance(file, UploadedFile):
        content = file.content
        name = file.filename if filename is None else filename
        media = file.content_type if content_type is None else content_type
    else:
        content = bytes(file)
        name = filename or ""
        media = content_type or "application/octet-stream"
    return content, _content_type(media), name


def _content_type(value: str) -> str:
    return core.normalize_content_type(value)


def _max_size(value: int | None, configured: int) -> int:
    if value is None:
        return configured
    _whole(value, "max_bytes", 0)
    return min(value, configured)


def _check_key(key: str) -> None:
    if not core.validate_storage_key(key):
        raise InvalidStorageKey


def _new_key() -> str:
    return core.new_storage_key()


def _raise_storage_error(exc: ValueError) -> None:
    code = str(exc)
    if code == "file_too_large":
        raise FileTooLarge from None
    if code == "quota_exceeded":
        raise StorageQuotaExceeded from None
    if code == "invalid_key":
        raise InvalidStorageKey from None
    if code == "not_found":
        raise StoredFileNotFound from None
    raise exc


class MemoryFileStorage(FileStorage):
    """Thread-safe, bounded storage for tests and single-worker development."""

    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MEMORY_MAX_BYTES,
    ) -> None:
        _whole(max_file_bytes, "max_file_bytes", 0)
        _whole(max_total_bytes, "max_total_bytes", 0)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._native = core.NativeMemoryStorage(max_file_bytes, max_total_bytes)

    def save_sync(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        content, media, name = _file_parts(file, content_type, filename)
        limit = _max_size(max_bytes, self.max_file_bytes)
        try:
            key, size, resolved_media, digest, stored_name = self._native.save(
                content, media, name, limit
            )
        except ValueError as exc:
            _raise_storage_error(exc)
            raise  # unreachable
        return StoredFile(key, size, resolved_media, digest, stored_name)

    async def save(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        return self.save_sync(
            file,
            content_type=content_type,
            filename=filename,
            max_bytes=max_bytes,
        )

    def read_sync(self, key: str, *, max_bytes: int | None = None) -> bytes:
        limit = _max_size(max_bytes, self.max_file_bytes)
        try:
            return self._native.read(key, limit)
        except ValueError as exc:
            _raise_storage_error(exc)
            raise  # unreachable

    async def read(self, key: str, *, max_bytes: int | None = None) -> bytes:
        return self.read_sync(key, max_bytes=max_bytes)

    def exists_sync(self, key: str) -> bool:
        try:
            return self._native.exists(key)
        except ValueError as exc:
            _raise_storage_error(exc)
            raise  # unreachable

    async def exists(self, key: str) -> bool:
        return self.exists_sync(key)

    def delete_sync(self, key: str) -> bool:
        try:
            return self._native.delete(key)
        except ValueError as exc:
            _raise_storage_error(exc)
            raise  # unreachable

    async def delete(self, key: str) -> bool:
        return self.delete_sync(key)

    async def open_response(
        self,
        key: str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        disposition: Literal["attachment", "inline"] = "attachment",
    ) -> Response:
        stored = self._metadata(key)
        shown = filename or stored.filename
        media = _response_media(content_type, stored.content_type, shown)
        return Response(
            body=self.read_sync(key),
            content_type=media,
            headers=_download_headers(disposition, shown, media),
        )

    def _metadata(self, key: str) -> StoredFile:
        try:
            key, size, media, digest, name = self._native.metadata(key)
        except ValueError as exc:
            _raise_storage_error(exc)
            raise  # unreachable
        return StoredFile(key, size, media, digest, name)


def _check_read_size(content: bytes, max_bytes: int | None) -> None:
    if max_bytes is not None:
        _whole(max_bytes, "max_bytes", 0)
        if len(content) > max_bytes:
            raise FileTooLarge


def _response_media(content_type: str | None, stored: str, shown: str) -> str:
    """Explicit type, then stored metadata, then a guess from the shown name."""
    if content_type:
        return _content_type(content_type)
    if stored != "application/octet-stream" or not shown:
        return stored
    return _content_type(mimetypes.guess_type(shown)[0] or stored)


def _download_headers(
    disposition: Literal["attachment", "inline"], shown: str, media: str
) -> dict[str, str]:
    """``inline`` survives only for media types that cannot script the origin."""
    return {"content-disposition": core.storage_disposition(disposition, shown, media)}


class LocalFileStorage(FileStorage):
    """Atomic local-disk backend; files are shared only through a shared volume."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int | None = None,
        workers: int = 2,
        max_pending: int = 32,
    ) -> None:
        _whole(max_file_bytes, "max_file_bytes", 0)
        if max_total_bytes is not None:
            _whole(max_total_bytes, "max_total_bytes", 0)
        _whole(workers, "workers", 1)
        _whole(max_pending, "max_pending", 0)
        self.root = Path(root)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.workers = workers
        self.max_pending = max_pending
        self._resolved_root: Path | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._slots: asyncio.Semaphore | None = None
        self._quota_lock = threading.Lock()
        self._size = 0
        self._reserved_bytes = 0

    async def start(self) -> None:
        pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="hinoki-storage"
        )
        self._pool = pool
        loop = asyncio.get_running_loop()
        try:
            resolved, size = await loop.run_in_executor(pool, self._prepare_root)
        except BaseException:
            await self.close()
            raise
        if self.max_total_bytes is not None and size > self.max_total_bytes:
            await self.close()
            raise StorageQuotaExceeded
        self._resolved_root = resolved
        self._size = size
        self._slots = asyncio.Semaphore(self.workers + self.max_pending)

    def _prepare_root(self) -> tuple[Path, int]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = self.root.resolve(strict=True)
        if not resolved.is_dir() or not os.access(resolved, os.W_OK):
            raise StorageUnavailable("configured storage root is not writable")
        return resolved, _directory_size(resolved)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            shutdown = asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(pool.shutdown, wait=True, cancel_futures=False),
            )
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                await shutdown
                raise
        self._slots = None
        self._resolved_root = None

    def _path(self, key: str, *, create_parent: bool = False) -> Path:
        _check_key(key)
        root = self._resolved_root
        if root is None:
            raise StorageUnavailable("storage module is not started")
        path = root / key
        parent = path.parent
        if create_parent:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _inside(root, path):
            raise InvalidStorageKey
        return path

    async def _run(self, operation: Callable[[], T]) -> T:
        pool, slots = self._pool, self._slots
        if pool is None or slots is None:
            raise StorageUnavailable("storage module is not started")
        if slots.locked():
            raise StorageUnavailable("storage worker queue is full")
        await slots.acquire()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(pool, operation)
        deferred_release = False
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if not future.done():
                future.add_done_callback(
                    lambda _: loop.call_soon_threadsafe(slots.release)
                )
                deferred_release = True
            raise
        finally:
            if not deferred_release:
                slots.release()

    async def save(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        return await self._run(
            functools.partial(
                self.save_sync,
                file,
                content_type=content_type,
                filename=filename,
                max_bytes=max_bytes,
            )
        )

    def save_sync(
        self,
        file: UploadedFile | bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredFile:
        content, media, name = _file_parts(file, content_type, filename)
        if len(content) > _max_size(max_bytes, self.max_file_bytes):
            raise FileTooLarge
        key = _new_key()
        target = self._path(key, create_parent=True)
        self._write_with_quota(target, content)
        return StoredFile(
            key, len(content), media, hashlib.sha256(content).hexdigest(), name
        )

    async def read(self, key: str, *, max_bytes: int | None = None) -> bytes:
        return await self._run(
            functools.partial(self.read_sync, key, max_bytes=max_bytes)
        )

    def read_sync(self, key: str, *, max_bytes: int | None = None) -> bytes:
        path = self._path(key)
        return _read_file(path, _max_size(max_bytes, self.max_file_bytes))

    async def exists(self, key: str) -> bool:
        exists = await self._run(functools.partial(self.exists_sync, key))
        return bool(exists)

    def exists_sync(self, key: str) -> bool:
        return self._path(key).is_file()

    async def delete(self, key: str) -> bool:
        deleted = await self._run(functools.partial(self.delete_sync, key))
        return bool(deleted)

    def delete_sync(self, key: str) -> bool:
        path = self._path(key)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageUnavailable from None
        with self._quota_lock:
            self._size = max(0, self._size - size)
        return True

    async def open_response(
        self,
        key: str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        disposition: Literal["attachment", "inline"] = "attachment",
    ) -> Response:
        path, size = await self._run(functools.partial(self._response_info, key))
        if size > self.max_file_bytes:
            raise FileTooLarge
        # Local files keep no metadata: the name and type come from the caller.
        shown = filename or ""
        media = _response_media(content_type, "application/octet-stream", shown)
        return File(
            path,
            root=self._root(),
            headers=_download_headers(disposition, shown, media),
            content_type=media,
        )

    def _root(self) -> Path:
        if self._resolved_root is None:
            raise StorageUnavailable("storage module is not started")
        return self._resolved_root

    def _response_info(self, key: str) -> tuple[Path, int]:
        path = self._path(key)
        return path, _file_size(path)

    def _write_with_quota(self, path: Path, content: bytes) -> None:
        with self._quota_lock:
            if (
                self.max_total_bytes is not None
                and self._size + self._reserved_bytes + len(content)
                > self.max_total_bytes
            ):
                raise StorageQuotaExceeded
            self._reserved_bytes += len(content)
        try:
            _atomic_write(path, content, self._root())
        except BaseException:
            with self._quota_lock:
                self._reserved_bytes -= len(content)
            raise
        with self._quota_lock:
            self._reserved_bytes -= len(content)
            self._size += len(content)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_write(path: Path, content: bytes, root: Path) -> None:
    temp_dir = root / ".tmp"
    if not _inside(root, temp_dir):
        raise StorageUnavailable
    temp_dir.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="upload-", dir=temp_dir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    except OSError:
        with suppress(OSError):
            os.unlink(name)
        raise StorageUnavailable from None


def _directory_size(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and _inside(root, path)
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        raise StoredFileNotFound from None
    except OSError:
        raise StorageUnavailable from None


def _read_file(path: Path, max_bytes: int) -> bytes:
    size = _file_size(path)
    if size > max_bytes:
        raise FileTooLarge
    try:
        with path.open("rb") as source:
            content = source.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise FileTooLarge
        return content
    except FileNotFoundError:
        raise StoredFileNotFound from None
    except OSError:
        raise StorageUnavailable from None


class FileStorageModule(Module):
    """Starts and closes the configured file backend once per worker."""

    name = "file-storage"

    def __init__(self, storage: FileStorage) -> None:
        self.storage = storage

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        if isinstance(self.storage, LocalFileStorage):
            await self.storage.start()
        try:
            ctx.provide(FileStorage, self.storage)
            if type(self.storage) is not FileStorage:
                ctx.provide(type(self.storage), self.storage)
            yield
        finally:
            if isinstance(self.storage, LocalFileStorage):
                await self.storage.close()


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MEMORY_MAX_BYTES",
    "FileStorage",
    "FileStorageModule",
    "FileTooLarge",
    "InvalidStorageKey",
    "LocalFileStorage",
    "MemoryFileStorage",
    "StorageError",
    "StorageQuotaExceeded",
    "StorageUnavailable",
    "StoredFile",
    "StoredFileNotFound",
]
