"""Coverage tests for mid-tier paths across resources, ORM, events, and llms."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hinoki import Hinoki, RuntimeConfig, http
from hinoki.ai.llms import render_llms, schema_name
from hinoki.auth import BaseAuth, Caller, Credentials
from hinoki.blocking import is_coroutine_hook
from hinoki.cache import (
    CacheModule,
    CacheUnavailable,
    DatabaseCache,
    _cache_db,
)
from hinoki.cache import (
    migrations_path as cache_migrations_path,
)
from hinoki.db import Database, DatabaseModule
from hinoki.errors import (
    EventRegistrationError,
    FieldError,
    MigrationError,
    RegistrationError,
)
from hinoki.events import check_handler, event_key
from hinoki.orm import (
    Model,
    OnDelete,
    field,
    foreign_key,
    soft_delete,
    timestamps,
    track_status,
    use_database,
)
from hinoki.orm.migrations import (
    alter_column,
    base_type,
    column_state,
    create_table_sql,
    dialect_type,
    quote,
    table_changes,
)
from hinoki.orm.model import FieldMeta
from hinoki.resources import (
    _auth_user_type,
    _caller_annotation_info,
    _coerce_uuid_scope,
    _concrete_types,
)
from hinoki.schema import Schema
from hinoki.sessions import (
    DatabaseSessionStore,
    SessionModule,
    SessionSettings,
    SessionUnavailable,
    _session_db,
)
from hinoki.testing import ASGIClient


class _PlainAuth(BaseAuth[Any]):
    async def authenticate(self, credentials: Credentials) -> Caller[Any] | None:
        del credentials
        return None


class _Parent(Model, table="cov_mid_parents"):
    id: int = field(primary_key=True)
    name: str


class _Child(Model, table="cov_mid_children"):
    id: int = field(primary_key=True)
    parent_id: int = foreign_key(_Parent, on_delete=OnDelete.RESTRICT)
    deleted_at: datetime | None = None
    name: str


class _ChildOut(Schema):
    id: int
    name: str


def test_resources_annotation_and_uuid_helpers() -> None:
    uid = UUID("11111111-1111-1111-1111-111111111111")
    assert _coerce_uuid_scope(uid) == uid
    assert _coerce_uuid_scope("not-a-uuid") is None

    assert _caller_annotation_info(Caller) == (True, False, Any)
    assert _caller_annotation_info(Caller[str] | None) == (True, True, str)
    assert _auth_user_type(_PlainAuth()) is Any
    assert _concrete_types(str | int) == (str, int)


def test_llms_markdown_and_array_schema() -> None:
    doc: dict[str, object] = {
        "info": {"title": "Demo", "description": "Hello API", "version": "1.0"},
        "paths": {},
        "x-hinoki-websockets": {
            "/ws": {"operationId": "stream", "summary": "Live updates"}
        },
    }
    md = render_llms(doc, openapi_url="/openapi.json")
    assert "> Hello API" in md
    assert "## WebSockets" in md
    assert "`WEBSOCKET /ws` (stream): Live updates" in md
    assert (
        schema_name({"type": "array", "items": {"$ref": "#/components/schemas/Item"}})
        == "list of Item"
    )


def test_migrations_error_paths_and_dialect_type() -> None:
    with pytest.raises(MigrationError):
        quote("bad;name")
    with pytest.raises(MigrationError):
        base_type(_Parent, "f", int | str)
    with pytest.raises(MigrationError):
        column_state(_Parent, "f", object, FieldMeta())
    assert isinstance(dialect_type("int", "postgres"), str)
    with pytest.raises(MigrationError):
        create_table_sql("bad;name", {}, "postgres")
    with pytest.raises(MigrationError):
        alter_column(
            "bad;name",
            "c",
            {"type": "int", "nullable": False},
            {"type": "text", "nullable": False},
            "postgres",
        )
    with pytest.raises(MigrationError):
        table_changes(
            "bad;name",
            {"columns": {"a": {"type": "int", "nullable": False}}},
            {"columns": {"a": {"type": "text", "nullable": False}}},
            "postgres",
        )


def test_events_registration_validation() -> None:
    with pytest.raises(EventRegistrationError):
        event_key(int | str)

    class _NotCallable:
        def __init__(self) -> None:
            self.__module__ = "m"
            self.__qualname__ = "q"

    with pytest.raises(EventRegistrationError):
        check_handler(_NotCallable(), (object, None), phase=None)  # type: ignore[arg-type]

    def bad_hints(ev: "MissingType") -> None:  # type: ignore[name-defined]  # noqa: F821, UP037
        del ev

    with pytest.raises(EventRegistrationError):
        check_handler(bad_hints, (object, None), phase=None)  # type: ignore[arg-type]


def test_orm_options_validation_errors() -> None:
    with pytest.raises(RegistrationError, match="string"):
        soft_delete(123)  # type: ignore[arg-type]

    with pytest.raises(RegistrationError, match="primary key"):

        @soft_delete("id")
        class _BadPk(Model, table="cov_bad_pk"):
            id: int = field(primary_key=True)

    with pytest.raises(RegistrationError, match="conflicts"):

        @soft_delete("updated_at")
        @timestamps()
        class _BadConflict(Model, table="cov_bad_conflict"):
            id: int = field(primary_key=True)
            created_at: datetime
            updated_at: datetime

    with pytest.raises(RegistrationError, match="callable"):

        @soft_delete("deleted_at", clock="nope")  # type: ignore[arg-type]
        class _BadClock(Model, table="cov_bad_clock"):
            id: int = field(primary_key=True)
            deleted_at: datetime | None = None

    with pytest.raises(RegistrationError, match="strings"):
        track_status(123)  # type: ignore[arg-type]

    with pytest.raises(RegistrationError):

        @track_status("status", changed_at="status")
        class _BadSame(Model, table="cov_bad_same"):
            id: int = field(primary_key=True)
            status: str = "a"

    with pytest.raises(RegistrationError, match="must be datetime"):

        @track_status("status", changed_at="other")
        class _BadKind(Model, table="cov_bad_kind"):
            id: int = field(primary_key=True)
            status: str = "a"
            other: str = "b"


async def test_queryset_soft_delete_join_errors_and_sync_wrappers(
    tmp_path: Path,
) -> None:
    db = await Database.connect(f"sqlite://{tmp_path / 'qs.sqlite3'}")
    try:
        await db.execute(
            "CREATE TABLE cov_mid_parents (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await db.execute(
            "CREATE TABLE cov_mid_children "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, deleted_at TEXT, name TEXT)"
        )
        await db.execute("INSERT INTO cov_mid_parents VALUES (1, 'p')")
        await db.execute("INSERT INTO cov_mid_children VALUES (1, 1, NULL, 'c')")

        with use_database(db):
            joined = _Child.filter(id=1, parent__name="p")
            with pytest.raises(FieldError, match="across a relation"):
                await joined.ahard_delete()
            row = await _Child.filter(id=1).aget_as(_ChildOut)
            assert row.name == "c"
    finally:
        db.close()


async def test_sync_orm_and_cache_wrappers(tmp_path: Path) -> None:
    db_url = f"sqlite://{tmp_path / 'qs_sync.sqlite3'}"
    db = await Database.connect(db_url)
    try:
        await db.execute(
            "CREATE TABLE cov_mid_parents (id INTEGER PRIMARY KEY, name TEXT)"
        )
        await db.execute("INSERT INTO cov_mid_parents VALUES (1, 'p')")
        sql_text = (cache_migrations_path() / "0001-initial.sql").read_text(
            encoding="utf-8"
        )
        sql_body = sql_text[sql_text.index("CREATE TABLE") :]
        for stmt in [s.strip() for s in sql_body.split(";") if s.strip()]:
            await db.execute(stmt)
    finally:
        db.close()

    cache = DatabaseCache()

    @http.get("/sync-check")
    def sync_check() -> dict[str, Any]:
        got = _Parent.filter(id=1).get_as(_ChildOut).name
        missing = _Parent.filter(id=999).get_or_none_as(_ChildOut)
        deleted = _Parent.filter(id=1).hard_delete()
        cache.set_bytes_sync("k", b"val", ttl=60)
        cached = cache.get_bytes_sync("k")
        removed = cache.delete_sync("k")
        return {
            "got": got,
            "missing": missing,
            "deleted": deleted,
            "cached": cached == b"val",
            "removed": removed,
        }

    app = Hinoki(
        runtime=RuntimeConfig(),
        include=[sync_check],
        modules=[DatabaseModule(db_url), CacheModule(cache)],
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    async with ASGIClient(app) as client:
        res = await client.get("/sync-check")
        assert res.status == 200
        assert res.body == {
            "got": "p",
            "missing": None,
            "deleted": 1,
            "cached": True,
            "removed": True,
        }


async def test_cache_and_session_module_lifespan_and_db_errors(tmp_path: Path) -> None:
    async def boom() -> int:
        raise RuntimeError("db down")

    with pytest.raises(CacheUnavailable):
        await _cache_db(boom())
    with pytest.raises(SessionUnavailable):
        await _session_db(boom())

    db_url = f"sqlite://{tmp_path / 'mod.sqlite3'}"
    cache = DatabaseCache()
    store = DatabaseSessionStore()
    app = Hinoki(
        runtime=RuntimeConfig(),
        modules=[
            DatabaseModule(db_url),
            CacheModule(cache),
            SessionModule(store, SessionSettings(csrf_secret=b"x" * 32)),
        ],
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    async with ASGIClient(app):
        assert cache._database is not None
        assert store._database is not None
    assert cache._database is None
    assert store._database is None

    class _SyncObj:
        def __call__(self) -> None:
            return None

    assert is_coroutine_hook(_SyncObj()) is False
    assert _PlainAuth().openapi_scheme() is None
