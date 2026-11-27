"""Targeted coverage tests for projection, pagination, storage, cache, and sessions."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest

from hinoki import Secret
from hinoki import auth as auth_mod
from hinoki.cache import (
    Cache,
    CacheDecodeError,
    CacheModule,
    CacheUnavailable,
    CacheValueTooLarge,
    DatabaseCache,
    MemoryCache,
    _json_bytes,
    _positive_limit,
    _schema_bytes,
)
from hinoki.cache import (
    migrations_path as cache_migrations_path,
)
from hinoki.db import Database
from hinoki.errors import Unauthorized, ValidationError
from hinoki.fields import Field
from hinoki.orm import Model, field, use_database
from hinoki.orm.projection import _ProjectedItem, _ProjectedList, _ProjectedPage
from hinoki.pagination import CursorPagination, PageNumberPagination
from hinoki.schema import Schema
from hinoki.sessions import (
    DatabaseSessionStore,
    Session,
    SessionAuth,
    SessionError,
    SessionModule,
    SessionRecord,
    SessionSettings,
    SessionStore,
    SessionUnavailable,
    _copy_record,
    _session_upsert,
)
from hinoki.sessions import (
    _datetime as session_datetime,
)
from hinoki.sessions import (
    migrations_path as session_migrations_path,
)
from hinoki.storage import (
    FileStorage,
    FileTooLarge,
    InvalidStorageKey,
    LocalFileStorage,
    MemoryFileStorage,
    StorageQuotaExceeded,
    StorageUnavailable,
    StoredFileNotFound,
    _atomic_write,
    _check_key,
    _check_read_size,
    _file_size,
    _inside,
    _max_size,
    _read_file,
    _whole,
)


class _ProjRow(Model, table="cov_proj_rows"):
    id: int = field(primary_key=True)
    name: str
    score: int


class _ProjRowOut(Schema):
    id: int
    name: str
    score: int


class _ConstrainedOut(Schema):
    id: int
    name: Annotated[str, Field(min_length=2)]
    score: int


class _SecretOut(Schema):
    id: int
    token: Secret


class _NestedSecretOut(Schema):
    id: int
    inner: _SecretOut


async def test_projected_list_and_page_methods(tmp_path: Path) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'proj.sqlite3'}")
    try:
        await db.execute(
            "CREATE TABLE cov_proj_rows "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, score INTEGER NOT NULL)"
        )
        await db.execute(
            "INSERT INTO cov_proj_rows (id, name, score) "
            "VALUES (1, 'ab', 10), (2, 'bc', 20)"
        )
        with use_database(db):
            plist = await _ProjRow.all().order_by("id").aproject(_ProjRowOut)
            assert isinstance(plist, _ProjectedList)
            assert bool(plist) is True
            assert len(list(reversed(plist))) == 2
            assert plist[0].name == "ab"
            assert len(plist[:1]) == 1
            first = _ProjRowOut(id=1, name="ab", score=10)
            assert first in plist
            assert "ab" in repr(plist)
            assert plist == [
                _ProjRowOut(id=1, name="ab", score=10),
                _ProjRowOut(id=2, name="bc", score=20),
            ]
            assert plist != []
            ops: tuple[Callable[[list[_ProjRowOut], list[_ProjRowOut]], bool], ...] = (
                lambda a, b: a < b,
                lambda a, b: a <= b,
                lambda a, b: a > b,
                lambda a, b: a >= b,
            )
            for op in ops:
                with pytest.raises(TypeError):
                    op(plist, [_ProjRowOut(id=0, name="z", score=0)])
            assert len(copy.copy(plist)) == 2
            assert len(copy.deepcopy(plist)) == 2
            assert len(plist.copy()) == 2

            extra = _ProjRowOut(id=3, name="c", score=30)
            plist.append(extra)
            plist.extend([extra])
            plist.insert(0, extra)
            assert plist.count(extra) == 3
            assert plist.index(extra) == 0
            plist[0] = first
            del plist[0]
            assert plist.pop() == extra
            plist.remove(extra)
            plist.reverse()
            plist.sort(key=lambda r: r.id)
            assert len(plist.__add__([extra])) == 3
            plist += [extra]
            assert len(plist * 2) == 6
            assert len(2 * plist) == 6
            plist *= 1
            plist.clear()
            assert bool(plist) is False

            page = await _ProjRow.all().order_by("id").apaginate_as(_ProjRowOut)
            assert isinstance(page, _ProjectedPage)
            assert page != "other"
            assert copy.copy(page) == page
            assert copy.deepcopy(page) == page
    finally:
        db.close()


async def test_projected_item_and_fallback_methods(tmp_path: Path) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'proj_item.sqlite3'}")
    try:
        await db.execute(
            "CREATE TABLE cov_proj_rows "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, score INTEGER NOT NULL)"
        )
        await db.execute(
            "INSERT INTO cov_proj_rows (id, name, score) VALUES (1, 'ab', 10)"
        )
        with use_database(db):
            first = _ProjRowOut(id=1, name="ab", score=10)
            item = await _ProjRow.filter(id=1).aget_as(_ProjRowOut)
            assert isinstance(item, _ProjectedItem)
            assert "ab" in repr(item)
            assert copy.copy(item) == first
            assert copy.deepcopy(item) == first
            broken = _ProjectedItem(
                item._projected_handle,  # type: ignore[arg-type]
                _ProjRowOut,
                lambda _h: first,
            )
            object.__setattr__(broken, "_projected_handle", None)
            with pytest.raises(RuntimeError, match="no backing row"):
                broken._ensure()

            fallback_one = await _ProjRow.filter(id=1).aget_or_none_as(_ConstrainedOut)
            assert fallback_one is not None and fallback_one.name == "ab"
            missing = await _ProjRow.filter(id=999).aget_or_none_as(_ConstrainedOut)
            assert missing is None
            with pytest.raises(_ProjRow.NotFound):
                await _ProjRow.filter(id=999).aget_as(_ConstrainedOut)
    finally:
        db.close()


def test_pagination_number_and_token_edge_cases() -> None:
    errors: list[Any] = []
    assert PageNumberPagination.number({}, "page", 1, 10, errors) == 1
    assert PageNumberPagination.number({"page": ["0"]}, "page", 1, 10, errors) == 1
    assert PageNumberPagination.number({"page": ["99"]}, "page", 1, 10, errors) == 1
    assert PageNumberPagination.number({"page": ["1" * 20]}, "page", 1, 10, errors) == 1
    assert PageNumberPagination.number({"page": ["abc"]}, "page", 1, 10, errors) == 1
    assert PageNumberPagination.number({"page": ["3"]}, "page", 1, 10, errors) == 3
    assert len(errors) == 4

    tok_errors: list[Any] = []
    assert CursorPagination.token({}, tok_errors) is None
    assert CursorPagination.token({"cursor": ["a", "b"]}, tok_errors) is None
    assert CursorPagination.token({"cursor": [""]}, tok_errors) is None
    assert CursorPagination.token({"cursor": ["tok123"]}, tok_errors) == "tok123"
    assert len(tok_errors) == 2

    with pytest.raises(ValidationError):
        PageNumberPagination().parse({"page": ["0"]})


async def test_storage_contract_and_memory_errors() -> None:
    base = FileStorage()
    for coro in (
        base.save(b"x"),
        base.read("k"),
        base.exists("k"),
        base.delete("k"),
        base.open_response("k"),
    ):
        with pytest.raises(NotImplementedError):
            await coro
    for fn in (
        lambda: base.save_sync(b"x"),
        lambda: base.read_sync("k"),
        lambda: base.exists_sync("k"),
        lambda: base.delete_sync("k"),
    ):
        with pytest.raises(NotImplementedError):
            fn()

    with pytest.raises(ValueError, match="integer"):
        _whole(True, "limit", 0)
    assert _max_size(5, 10) == 5
    with pytest.raises(InvalidStorageKey):
        _check_key("../bad")
    with pytest.raises(FileTooLarge):
        _check_read_size(b"hello", 2)

    mem = MemoryFileStorage(max_file_bytes=10)
    for fn in (
        lambda: mem.read_sync("../bad"),
        lambda: mem.exists_sync("../bad"),
        lambda: mem.delete_sync("../bad"),
        lambda: mem._metadata("../bad"),
    ):
        with pytest.raises(InvalidStorageKey):
            fn()


async def test_local_storage_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = LocalFileStorage(tmp_path / "store", max_file_bytes=5, max_total_bytes=4)
    with pytest.raises(StorageUnavailable, match="not started"):
        local._path("abc")
    with pytest.raises(StorageUnavailable, match="not started"):
        local._root()
    with pytest.raises(StorageUnavailable, match="not started"):
        await local.read("abc")

    seed_dir = tmp_path / "over_quota"
    seed_dir.mkdir()
    (seed_dir / "big.bin").write_bytes(b"123456")
    over = LocalFileStorage(seed_dir, max_total_bytes=4)
    with pytest.raises(StorageQuotaExceeded):
        await over.start()

    file_not_dir = tmp_path / "not_a_dir"
    file_not_dir.write_bytes(b"x")
    bad_root = LocalFileStorage(file_not_dir)
    with pytest.raises((StorageUnavailable, FileExistsError, OSError)):
        await bad_root.start()

    good_dir = tmp_path / "good"
    good = LocalFileStorage(good_dir, max_file_bytes=4, workers=1, max_pending=0)
    await good.start()
    try:
        assert good.delete_sync("missing_key") is False
        with pytest.raises(StoredFileNotFound):
            await good.open_response("missing_key")
        (good_dir / "bigfile").write_bytes(b"123456")
        with pytest.raises(FileTooLarge):
            await good.open_response("bigfile")
        with pytest.raises(FileTooLarge):
            _read_file(good_dir / "bigfile", 4)
        with pytest.raises(StoredFileNotFound):
            _file_size(good_dir / "nope")
        with pytest.raises(StoredFileNotFound):
            _read_file(good_dir / "nope", 4)
        assert _inside(good_dir, Path("/etc/passwd")) is False

        monkeypatch.setattr(
            Path,
            "unlink",
            lambda _self: (_ for _ in ()).throw(OSError("disk")),
        )
        with pytest.raises(StorageUnavailable):
            good.delete_sync("bigfile")

        monkeypatch.setattr(
            "os.replace",
            lambda _src, _dst: (_ for _ in ()).throw(OSError("replace")),
        )
        with pytest.raises(StorageUnavailable):
            _atomic_write(good_dir / "x", b"1", good_dir)
    finally:
        await good.close()


async def test_cache_contract_and_memory_edge_cases() -> None:
    base = Cache()
    for coro in (
        base.get_bytes("k"),
        base.set_bytes("k", b"1", ttl=1),
        base.delete("k"),
    ):
        with pytest.raises(NotImplementedError):
            await coro
    for fn in (
        lambda: base.get_bytes_sync("k"),
        lambda: base.set_bytes_sync("k", b"1", ttl=1),
        lambda: base.delete_sync("k"),
        lambda: base.set_sync("k", {"a": 1}, ttl=1),
    ):
        with pytest.raises(NotImplementedError):
            fn()

    with pytest.raises(ValueError, match="positive integer"):
        _positive_limit(True, "n")
    with pytest.raises(TypeError, match="JSON serializable"):
        _json_bytes(object())
    with pytest.raises(TypeError, match="Schema instance"):
        _schema_bytes("not-a-schema")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Secret field"):
        _schema_bytes(_NestedSecretOut(id=1, inner=_SecretOut(id=1, token=Secret("x"))))
    with pytest.raises(ValueError, match="within max_bytes"):
        MemoryCache(max_entries=10, max_bytes=10, max_value_bytes=20)

    mem = MemoryCache()
    ns = mem.namespace("ns")
    ns.set_bytes_sync("k1", b"v1", ttl=60)
    assert ns.get_bytes_sync("k1") == b"v1"
    assert ns.delete_sync("k1") is True
    assert await mem.get_schema("missing", _ProjRowOut) is None
    with pytest.raises(AttributeError):
        _ = ns._private  # type: ignore[attr-defined]


async def test_database_cache_corrupt_and_expired_rows(tmp_path: Path) -> None:
    db_cache = DatabaseCache(max_value_bytes=4)
    with pytest.raises(CacheUnavailable):
        _ = db_cache.database
    db = await Database.connect(f"sqlite://{tmp_path / 'cache.sqlite3'}")
    try:
        sql_text = (cache_migrations_path() / "0001-initial.sql").read_text(
            encoding="utf-8"
        )
        sql_body = sql_text[sql_text.index("CREATE TABLE") :]
        for stmt in [s.strip() for s in sql_body.split(";") if s.strip()]:
            await db.execute(stmt)
        db_cache.bind(db)
        await db_cache.set_bytes("ok", b"hi", ttl=60)
        assert await db_cache.get_bytes("ok") == b"hi"
        assert await db_cache.delete("ok") is True

        past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
        ins = (
            "INSERT INTO hinoki_cache (cache_key, value_hex, expires_at) "
            "VALUES (?, ?, ?)"
        )
        await db.execute(ins, "exp", b"1".hex(), past)
        assert await db_cache.get_bytes("exp") is None
        await db.execute(ins, "badhex", "zzzz", future)
        with pytest.raises(CacheDecodeError):
            await db_cache.get_bytes("badhex")
        await db.execute(ins, "big", b"123456".hex(), future)
        with pytest.raises(CacheValueTooLarge):
            await db_cache.get_bytes("big")
        await db.execute(ins, "badts", b"1".hex(), "not-a-timestamp")
        with pytest.raises(CacheDecodeError):
            await db_cache.get_bytes("badts")
        await db_cache.purge_expired()
        db_cache.unbind()
        assert CacheModule(DatabaseCache()).requires != ()
    finally:
        db.close()


async def test_sessions_store_and_settings_edge_cases(tmp_path: Path) -> None:
    store = SessionStore()
    for coro in (
        store.load("h"),
        store.save(
            "h",
            SessionRecord(
                {},
                datetime.now(UTC),
                datetime.now(UTC),
                datetime.now(UTC),
                datetime.now(UTC),
            ),
        ),
        store.delete("h"),
        store.delete_by_subject("s"),
        store.purge_expired(),
    ):
        with pytest.raises(NotImplementedError):
            await coro

    rec = SessionRecord(
        {"k": "v"},
        datetime.now(UTC),
        datetime.now(UTC),
        datetime.now(UTC),
        datetime.now(UTC),
        "sub",
    )
    assert _copy_record(rec).subject == "sub"

    with pytest.raises(ValueError, match="positive"):
        SessionSettings(max_bytes=0, csrf_secret=b"x" * 32)
    assert session_datetime(datetime.now(UTC)).tzinfo is not None
    assert session_datetime("2026-01-01T00:00:00+00:00").year == 2026
    with pytest.raises(TypeError, match="invalid timestamp"):
        session_datetime(123)
    assert "ON DUPLICATE KEY" in _session_upsert("mysql")

    db_store = DatabaseSessionStore()
    with pytest.raises(SessionUnavailable):
        _ = db_store.database
    assert (
        SessionModule(db_store, SessionSettings(csrf_secret=b"x" * 32)).requires != ()
    )

    db = await Database.connect(f"sqlite://{tmp_path / 'sess.sqlite3'}")
    try:
        sql_text = (session_migrations_path() / "0001-initial.sql").read_text(
            encoding="utf-8"
        )
        sql_body = sql_text[sql_text.index("CREATE TABLE") :]
        for stmt in [s.strip() for s in sql_body.split(";") if s.strip()]:
            await db.execute(stmt)
        db_store.bind(db)
        now = datetime.now(UTC)
        ins = (
            "INSERT INTO hinoki_sessions (token_hash, data, created_at, "
            "expires_at, idle_expires_at, last_seen_at, subject, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)"
        )
        await db.execute(
            ins,
            "badjson",
            "{not-json",
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            now.isoformat(),
            "u1",
        )
        with pytest.raises(SessionUnavailable):
            await db_store.load("badjson")
        await db.execute(
            ins,
            "notdict",
            "[1, 2]",
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            now.isoformat(),
            "u1",
        )
        with pytest.raises(SessionUnavailable):
            await db_store.load("notdict")
        db_store.unbind()
    finally:
        db.close()


async def test_session_object_and_auth_edge_cases() -> None:
    db_store = DatabaseSessionStore()
    settings = SessionSettings(csrf_secret=b"x" * 32)
    expired_rec = SessionRecord(
        {"a": 1},
        datetime.now(UTC) - timedelta(hours=2),
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) - timedelta(hours=1),
        datetime.now(UTC) - timedelta(hours=2),
    )
    sess = Session(store=db_store, settings=settings, token=None, record=expired_rec)
    assert sess["a"] == 1
    del sess["a"]
    sess.clear()
    with pytest.raises(ValueError, match="255"):
        sess.subject = "x" * 256
    sess.subject = "ok"
    with pytest.raises(SessionError, match="no token"):
        sess.csrf_token()
    with pytest.raises(SessionError, match="no token"):
        sess.verify_csrf("abc")
    with pytest.raises(SessionUnavailable, match="expired"):
        await sess.commit()

    sauth: SessionAuth[None] = SessionAuth(lambda _data: None)
    with pytest.raises(Unauthorized):
        await sauth.authenticate(auth_mod.Credentials({}, {}, None))
