"""Bounded in-memory and local file storage backends."""

import asyncio
from pathlib import Path

import pytest

from hinoki import Hinoki, http
from hinoki.parsers import UploadedFile
from hinoki.storage import (
    FileStorage,
    FileStorageModule,
    FileTooLarge,
    InvalidStorageKey,
    LocalFileStorage,
    MemoryFileStorage,
    StorageQuotaExceeded,
    StoredFile,
    StoredFileNotFound,
)
from hinoki.testing import ASGIClient


@http.post("/store")
async def store(file: UploadedFile, storage: FileStorage) -> dict[str, object]:
    saved = await storage.save(file)
    assert await storage.read(saved.key) == file.content
    return {"key": saved.key, "size": saved.size, "sha256": saved.sha256}


@http.post("/store-sync")
def store_sync(file: UploadedFile, storage: FileStorage) -> dict[str, object]:
    saved = storage.save_sync(file)
    assert storage.read_sync(saved.key) == file.content
    return {"key": saved.key, "size": saved.size}


async def test_memory_storage_limits_and_idempotent_delete() -> None:
    storage = MemoryFileStorage(max_file_bytes=4, max_total_bytes=4)
    uploaded = UploadedFile("document", "../report.txt", "text/plain", b"data")
    saved = await storage.save(uploaded)
    assert saved.filename == "../report.txt"
    assert saved.content_type == "text/plain"
    assert await storage.read(saved.key) == b"data"
    assert await storage.exists(saved.key)
    with pytest.raises(StorageQuotaExceeded):
        await storage.save(b"x")
    assert await storage.delete(saved.key)
    assert not await storage.delete(saved.key)
    assert not await storage.exists(saved.key)
    empty = await storage.save(b"")
    assert empty.size == 0


def test_memory_storage_rejects_oversize_and_unsafe_keys() -> None:
    storage = MemoryFileStorage(max_file_bytes=2, max_total_bytes=8)
    with pytest.raises(FileTooLarge):
        storage.save_sync(b"too long")
    for key in ("../secret", "/etc/passwd", "a/../b", "nul\x00key"):
        with pytest.raises(InvalidStorageKey):
            storage.read_sync(key)
    with pytest.raises(StoredFileNotFound):
        storage.read_sync("ab/cd/" + "0" * 32)


async def test_local_storage_runs_through_module_and_supports_sync_handlers(
    tmp_path: Path,
) -> None:
    backend = LocalFileStorage(tmp_path / "uploads", max_file_bytes=32)
    app = Hinoki(include=[store, store_sync], modules=[FileStorageModule(backend)])
    async with ASGIClient(app) as client:
        for route in ("/store", "/store-sync"):
            response = await client.post(
                route, files=[("file", "report.txt", b"payload", "text/plain")]
            )
            assert response.status == 200
            assert response.body["size"] == 7
            path = backend._path(str(response.body["key"]))
            assert path.read_bytes() == b"payload"
    assert backend._pool is None


async def test_local_storage_creates_atomic_files_and_returns_file_response(
    tmp_path: Path,
) -> None:
    backend = LocalFileStorage(tmp_path / "files")
    await backend.start()
    try:
        first, second = await asyncio_gather_saves(backend)
        assert first.key != second.key
        assert await backend.read(first.key) == b"one"
        assert await backend.read(second.key) == b"two"
        answer = await backend.open_response(first.key, filename="download.txt")
        assert answer.headers[0] == (
            b"content-disposition",
            b'attachment; filename="download.txt"',
        )
        assert await backend.delete(first.key)
        assert not await backend.exists(first.key)
    finally:
        await backend.close()


async def asyncio_gather_saves(
    backend: LocalFileStorage,
) -> tuple[StoredFile, StoredFile]:
    first, second = await asyncio.gather(backend.save(b"one"), backend.save(b"two"))
    return first, second


async def test_local_quota_and_startup_failure(tmp_path: Path) -> None:
    backend = LocalFileStorage(tmp_path / "quota", max_total_bytes=2)
    await backend.start()
    try:
        await backend.save(b"12")
        with pytest.raises(StorageQuotaExceeded):
            await backend.save(b"3")
    finally:
        await backend.close()

    root_file = tmp_path / "not-a-directory"
    root_file.write_text("x")
    broken = Hinoki(
        include=[store],
        modules=[FileStorageModule(LocalFileStorage(root_file))],
    )
    with pytest.raises(RuntimeError, match="startup failed"):
        async with ASGIClient(broken):
            pytest.fail("a file cannot be used as the storage root")
