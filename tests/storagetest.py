"""Bounded in-memory and local file storage backends."""

import asyncio
import time
from pathlib import Path
from threading import Event
from urllib.parse import urlencode

import pytest

from hinoki import Hinoki, http
from hinoki.http.response import Response
from hinoki.parsers import UploadedFile
from hinoki.storage import (
    FileStorage,
    FileStorageModule,
    FileTooLarge,
    InvalidStorageKey,
    LocalFileStorage,
    MemoryFileStorage,
    StorageQuotaExceeded,
    StorageUnavailable,
    StoredFile,
    StoredFileNotFound,
    _atomic_write,
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


@http.get("/documents")
async def download_document(key: str, storage: FileStorage) -> Response:
    return await storage.open_response(key)


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


@pytest.mark.parametrize("backend_kind", ["memory", "local"])
async def test_open_response_contract_works_over_http_for_each_backend(
    tmp_path: Path, backend_kind: str
) -> None:
    backend: FileStorage = (
        MemoryFileStorage()
        if backend_kind == "memory"
        else LocalFileStorage(tmp_path / "http-downloads")
    )
    app = Hinoki(
        include=[store, download_document], modules=[FileStorageModule(backend)]
    )
    async with ASGIClient(app) as client:
        uploaded = await client.post(
            "/store", files=[("file", "report.txt", b"payload", "text/plain")]
        )
        downloaded = await client.get(
            "/documents", query=urlencode({"key": uploaded.body["key"]})
        )
        assert uploaded.status == downloaded.status == 200
        assert downloaded.raw == b"payload"


async def test_local_startup_io_does_not_block_the_worker_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalFileStorage(tmp_path / "responsive")
    original = backend._prepare_root

    def slow_prepare() -> tuple[Path, int]:
        time.sleep(0.05)
        return original()

    monkeypatch.setattr(backend, "_prepare_root", slow_prepare)
    heartbeat = asyncio.Event()
    loop = asyncio.get_running_loop()
    scheduled = loop.time() + 0.005
    lag = 0.0

    def mark_heartbeat() -> None:
        nonlocal lag
        lag = loop.time() - scheduled
        heartbeat.set()

    loop.call_later(0.005, mark_heartbeat)
    try:
        start = asyncio.create_task(backend.start())
        await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
        await start
        assert lag < 0.03
    finally:
        await backend.close()


def test_local_read_checks_size_before_returning_data(tmp_path: Path) -> None:
    async def run() -> None:
        backend = LocalFileStorage(tmp_path / "bounded", max_file_bytes=4)
        await backend.start()
        try:
            stored = await backend.save(b"ok")
            backend._path(stored.key).write_bytes(b"too large")
            with pytest.raises(FileTooLarge):
                await backend.read(stored.key)
        finally:
            await backend.close()

    asyncio.run(run())


async def test_failed_write_releases_reserved_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalFileStorage(tmp_path / "quota-release", max_total_bytes=4)
    await backend.start()

    def fail_write(_path: Path, _content: bytes, _root: Path) -> None:
        raise StorageUnavailable

    monkeypatch.setattr("hinoki.storage._atomic_write", fail_write)
    try:
        with pytest.raises(StorageUnavailable):
            await backend.save(b"1234")
        assert backend._reserved_bytes == 0
        monkeypatch.undo()
        assert (await backend.save(b"1234")).size == 4
    finally:
        await backend.close()


async def test_quota_lock_is_released_during_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalFileStorage(
        tmp_path / "quota-contention", max_total_bytes=4, workers=2
    )
    await backend.start()
    entered, release, contender_started = Event(), Event(), Event()

    def blocked_write(path: Path, content: bytes, root: Path) -> None:
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test write was not released")
        _atomic_write(path, content, root)

    monkeypatch.setattr("hinoki.storage._atomic_write", blocked_write)

    async def contend() -> StoredFile:
        contender_started.set()
        return await backend.save(b"x")

    first = asyncio.create_task(backend.save(b"1234"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(contend())
        assert await asyncio.to_thread(contender_started.wait, 1)
        done, _ = await asyncio.wait({second}, timeout=0.2)
        assert second in done
        with pytest.raises(StorageQuotaExceeded):
            await second
    finally:
        release.set()
        await first
        await backend.close()


async def test_close_waits_for_inflight_local_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalFileStorage(tmp_path / "close-waits")
    await backend.start()
    entered, release = Event(), Event()

    def blocked_write(path: Path, content: bytes, root: Path) -> None:
        entered.set()
        if not release.wait(2):
            raise TimeoutError("test write was not released")
        _atomic_write(path, content, root)

    monkeypatch.setattr("hinoki.storage._atomic_write", blocked_write)
    write = asyncio.create_task(backend.save(b"payload"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(backend.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert backend._resolved_root is not None
        release.set()
        await write
        await closing
        assert backend._resolved_root is None
        assert backend._pool is None
    finally:
        release.set()
        if not write.done():
            await write
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
