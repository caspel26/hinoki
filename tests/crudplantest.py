"""Startup compilation for the private native CRUD plans."""

import asyncio
import dataclasses
import enum
import json
import math
import os
import re
import sys
import types
import warnings
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, ClassVar, Literal, cast
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    Access,
    BearerAuth,
    Caller,
    CrudAccess,
    Field,
    Hinoki,
    Mount,
    RuntimeConfig,
    Schema,
    SchemaExtra,
    Unset,
    core,
    db,
    http,
)
from hinoki import resources as crud_module
from hinoki.__main__ import main
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Conflict
from hinoki.fields import UNSET
from hinoki.filters import Eq, FilterSet, In, Range, Search
from hinoki.http.request import Request
from hinoki.modules import ModuleContext
from hinoki.orm import (
    ManyToMany,
    Model,
    QuerySet,
    SoftDeleteModel,
    UUIDModel,
    field,
    many_to_many,
    urlsafe_token,
    uuid_pk,
)
from hinoki.orm.migrations import create_table_sql, table_state
from hinoki.orm.queryset import write_statement
from hinoki.pagination import CursorPagination, PageNumberPagination
from hinoki.resources import CrudBase, all_rows, caller_id, crud
from hinoki.runtime import crudplan as crudplan_module
from hinoki.runtime import servercore as servercore_module
from hinoki.runtime.binding import Binding
from hinoki.runtime.crudplan import CrudPlanSelection, select_crud_plan
from hinoki.runtime.kiso import KisoWorker, _drain_native_uninterruptibly
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.responses import Response
from hinoki.runtime.servercore import build_server_core
from hinoki.settings import AppSettings, CorsSettings, ServerSettings
from hinoki.testing import ASGIClient
from hinoki.utils import utc_now

# Fixtures declare owner= resources on purpose: the owner read-scope warning is
# not what these tests check.
OWNER_READS = r"\w+. owner=.* constrains writes only"
pytestmark = pytest.mark.filterwarnings(f"ignore:{OWNER_READS}:UserWarning")
warnings.filterwarnings(
    "ignore", message=OWNER_READS, category=UserWarning, module=__name__
)

DATABASE_URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
REQUIRE_SQL_TRACE = os.environ.get("HINOKI_REQUIRE_SQL_TRACE") == "1"


class PlanItem(Model, table="crud_plan_items"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    label: str = "model-default"


@db.soft_delete()
class SoftPlanItem(Model, table="crud_soft_plan_items"):
    id: int = field(primary_key=True)
    name: str
    deleted_at: datetime | None = None


@db.track_status()
class TrackedPlanItem(Model, table="crud_tracked_plan_items"):
    id: int = field(primary_key=True)
    status: str
    status_changed_at: datetime


@db.soft_delete()
class SoftCrudItem(Model, table="crud_soft_items"):
    id: int = field(primary_key=True)
    name: str
    deleted_at: datetime | None = None


@db.soft_delete()
class SoftTag(Model, table="crud_soft_tags"):
    id: int = field(primary_key=True)
    name: str
    deleted_at: datetime | None = None


class LinkedCrudItem(Model, table="crud_linked_items"):
    id: int = field(primary_key=True)
    name: str
    tags: ClassVar[ManyToMany[SoftTag]] = many_to_many(SoftTag, ids_name="tag_ids")


class SoftCrudItemOut(Schema):
    id: int
    name: str


class LinkedCrudItemOut(Schema):
    id: int
    name: str
    tag_ids: set[int]


LinkedCrudAPI = crud(
    LinkedCrudItem,
    path="/linked-items",
    read=LinkedCrudItemOut,
    links={"tags": all_rows(SoftTag)},
).api()


SoftCrudAPI = crud(
    SoftCrudItem,
    path="/soft-items",
    read=SoftCrudItemOut,
    delete=True,
    public_writes=True,
).api()


class SoftSyncAPI(API):
    @http.post("/soft-items/{item_id}/sync-delete")
    def delete_soft_item(self, request: Request, item_id: int) -> dict[str, bool]:
        row = SoftCrudItem.get(id=item_id)
        row.soft_delete()
        return {"deleted": row.is_deleted}

    @http.post("/soft-items/{item_id}/sync-restore")
    def restore_soft_item(self, request: Request, item_id: int) -> dict[str, bool]:
        row = SoftCrudItem.with_deleted().filter(id=item_id).get()
        row.restore()
        return {"deleted": row.is_deleted}


class PlanItemOut(Schema):
    id: UUID
    name: str
    label: str


class PlanItemIn(Schema):
    name: str
    label: str = "schema-default"


class PlanItemPatch(Schema):
    name: str | Unset = UNSET
    label: str | Unset = UNSET


PlanItems = crud(
    PlanItem,
    path="/plan-items",
    read=PlanItemOut,
    create=PlanItemIn,
    update=PlanItemPatch,
    delete=True,
    public_writes=True,
).api()


def unique_to_conflict(error: core.UniqueViolation) -> Conflict:
    assert error.constraint == "plan_items_name_key"
    assert error.table == "plan_items"
    assert error.sqlstate == "23505"
    return Conflict("A unique value is already in use.", code="unique_conflict")


def plan_pipeline() -> Pipeline:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    return Pipeline(app)


def test_public_crud_actions_get_disarmed_plans() -> None:
    pipeline = plan_pipeline()
    server = build_server_core(pipeline)

    selected = [item for item in pipeline.crud_plan_selections if item is not None]
    assert [item.action for item in selected] == [
        "list",
        "retrieve",
        "create",
        "update",
        "delete",
    ]
    assert all(item.snapshot_valid() for item in selected)
    assert server.pending == 0


def test_function_form_keeps_native_plan_eligibility() -> None:
    declared = crud(
        PlanItem,
        path="/fn-items",
        read=PlanItemOut,
        create=PlanItemIn,
        update=PlanItemPatch,
        delete=True,
        public_writes=True,
    )
    app = Hinoki(
        include=[declared],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    selected = [item for item in pipeline.crud_plan_selections if item is not None]
    assert [item.action for item in selected] == [
        "list",
        "retrieve",
        "create",
        "update",
        "delete",
    ]


def test_lifecycle_models_fail_closed_on_native_crud_lane() -> None:
    assert not crudplan_module.plain_crud_model(
        TrackedPlanItem, cast(Any, None), "update"
    )
    pipeline = Pipeline(
        Hinoki(
            include=[LinkedCrudAPI],
            modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
        )
    )
    candidate, _reason = crudplan_module.crud_candidate(pipeline.bindings[0])
    assert candidate is not None
    assert crudplan_module.native_links(candidate.resource) is None
    selection, _reason = select_crud_plan(pipeline.bindings[0], pipeline)
    assert selection is None


async def test_generated_crud_delete_soft_deletes_and_reads_as_404(
    tmp_path: Path,
) -> None:
    url = f"sqlite://{tmp_path / 'crud-soft-delete.sqlite3'}"
    database = await Database.connect(url)
    await database.execute(
        "CREATE TABLE crud_soft_items (id integer PRIMARY KEY, name text NOT NULL, "
        "deleted_at text)"
    )
    await database.execute("INSERT INTO crud_soft_items VALUES (1, 'visible', NULL)")
    await database.execute("INSERT INTO crud_soft_items VALUES (2, 'sync', NULL)")
    database.close()

    app = Hinoki(include=[SoftCrudAPI, SoftSyncAPI], modules=[DatabaseModule(url)])
    async with ASGIClient(app) as client:
        deleted = await client.request("DELETE", "/soft-items/1")
        hidden = await client.request("GET", "/soft-items/1")
        sync_deleted = await client.request("POST", "/soft-items/2/sync-delete")
        sync_hidden = await client.request("GET", "/soft-items/2")
        sync_restored = await client.request("POST", "/soft-items/2/sync-restore")
        sync_visible = await client.request("GET", "/soft-items/2")
    assert (deleted.status, hidden.status) == (204, 404)
    assert (sync_deleted.status, sync_deleted.body) == (200, {"deleted": True})
    assert sync_hidden.status == 404
    assert (sync_restored.status, sync_restored.body) == (200, {"deleted": False})
    assert sync_visible.status == 200


def test_unprefixed_retrieve_route_matches_its_generated_suffix() -> None:
    route = SimpleNamespace(
        method="GET",
        path="/items/{item_id}",
        handler=crudplan_module.ACTION_HANDLERS["retrieve"],
    )
    owner = SimpleNamespace(prefix="")

    assert crudplan_module.route_matches_action(
        route, "retrieve", cast(type[CrudBase], owner)
    )


def test_plan_templates_match_python_compilers_for_all_dialects() -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    plan = next(
        item.plan
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "list"
    )
    templates = {(dialect, kind): sql for dialect, kind, sql in plan.templates()}

    for dialect in ("postgres", "sqlite", "mysql", "mariadb"):
        query = QuerySet(PlanItem)
        ordered = query.order_by("id")
        assert templates[dialect, "count"] == query.compile("count", dialect=dialect)[0]
        assert (
            templates[dialect, "list"]
            == ordered.limit(20).offset(0).compile(dialect=dialect)[0]
        )
        by_id = query.filter(id=UUID(int=1)).limit(2)
        assert templates[dialect, "retrieve"] == by_id.compile(dialect=dialect)[0]
        assert templates[dialect, "read_back"] == by_id.compile(dialect=dialect)[0]
        assert (
            templates[dialect, "write_read_back"]
            == query.filter(id=UUID(int=1)).compile(dialect=dialect)[0]
        )
        if dialect in {"mysql", "mariadb"}:
            assert templates[dialect, "savepoint"] == "SAVEPOINT hinoki_sp_1"
            assert (
                templates[dialect, "rollback_savepoint"]
                == "ROLLBACK TO SAVEPOINT hinoki_sp_1"
            )
            assert (
                templates[dialect, "release_savepoint"]
                == "RELEASE SAVEPOINT hinoki_sp_1"
            )
        assert (
            templates[dialect, "create"]
            == write_statement(
                PlanItem,
                "insert",
                [(name, None) for name in PlanItem.__columns__],
                [],
                returning=True,
                dialect=dialect,
            )[0]
        )
        assert (
            templates[dialect, "update"]
            == write_statement(
                PlanItem,
                "update",
                [("name", None), ("label", None)],
                [("id", "eq", [UUID(int=1)])],
                returning=True,
                dialect=dialect,
            )[0]
        )
        assert (
            templates[dialect, "delete"]
            == query.filter(id=UUID(int=1)).compile("delete", dialect=dialect)[0]
        )
    assert plan.model_defaults() == [
        ("id", "uuid4", None),
        ("name", "required", None),
        ("label", "str", "model-default"),
    ]
    assert plan.create_defaults == [("label", "str", "schema-default")]
    assert plan.update_columns == ["name", "label"]


def test_list_statement_for_any_window_matches_the_python_compiler() -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    plan = next(
        item.plan
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "list"
    )
    for dialect in ("postgres", "sqlite", "mysql", "mariadb"):
        for size, offset in ((7, 14), (1, 0), (100, 99_900)):
            expected = (
                QuerySet(PlanItem)
                .order_by("id")
                .limit(size)
                .offset(offset)
                .compile(dialect=dialect)
            )
            assert plan.list_sql(dialect, size, offset) == expected


def test_plan_template_error_body_is_canonical_and_self_checked() -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    plan = next(item.plan for item in pipeline.crud_plan_selections if item)
    body = plan.validation_error_template
    assert body.count(b"0123456789abcdeffedcba9876543210") == 1
    assert b'"error":"validation_error"' in body


def test_mutated_model_snapshot_invalidates_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selection = next(item for item in pipeline.crud_plan_selections if item)
    monkeypatch.setattr(PlanItem, "__clock__", lambda: None)
    assert not selection.snapshot_valid()


@pytest.mark.parametrize("mutation", ["pagination", "crud_helper"])
def test_read_snapshot_tracks_python_lane_helpers(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selection = next(
        item
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "list"
    )
    if mutation == "pagination":
        monkeypatch.setattr(
            PageNumberPagination,
            "parse",
            lambda _self, _query: None,
        )
    else:
        monkeypatch.setattr(
            crud_module,
            "schema_items",
            lambda _self, _rows: [],
        )
    assert not selection.snapshot_valid()


@pytest.mark.parametrize("mutation", ["schema_config", "schema_default"])
def test_schema_snapshot_tracks_config_and_dataclass_defaults(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selection = next(
        item
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "create"
    )
    if mutation == "schema_config":
        changed = dataclasses.replace(
            PlanItemIn.__schema_config__, extra=SchemaExtra.IGNORE
        )
        monkeypatch.setattr(PlanItemIn, "__schema_config__", changed)
    else:
        field_info = PlanItemIn.__dataclass_fields__["label"]
        monkeypatch.setattr(field_info, "default", "changed-default")
    assert not selection.snapshot_valid()


def test_missing_database_has_a_stable_exclusion_reason() -> None:
    app = Hinoki(include=[PlanItems])
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    assert all(item is None for item in pipeline.crud_plan_selections)
    assert set(pipeline.crud_plan_reasons) == {"database_module_missing_or_custom"}


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("owner", "custom_route_or_access_policy"),
        ("model", "custom_or_unsupported_model"),
        ("schema", "custom_or_unsupported_schema"),
        ("dialect", "database_dialect_unsupported"),
        ("not_found_handler", "database_methods_or_not_found_handler_customized"),
        ("compiler", "native_plan_build_failed"),
    ],
)
def test_ineligible_routes_have_stable_reasons(
    mutation: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = [DatabaseModule("sqlite:///crud-plan-unused.sqlite3")]
    if mutation == "dialect":
        modules = [DatabaseModule("oracle://localhost/test")]
    app = Hinoki(include=[PlanItems], modules=modules)
    if mutation == "not_found_handler":
        app.errors.handlers[PlanItem.NotFound] = lambda error: error
    pipeline = Pipeline(app)
    binding = pipeline.bindings[0]
    if mutation == "owner":
        monkeypatch.setattr(PlanItems, "store", lambda _self, *_args: None)
    elif mutation == "model":
        monkeypatch.setattr(PlanItem, "__json_columns__", ("name",))
    elif mutation == "schema":
        monkeypatch.setattr(PlanItemOut, "__checks__", (("name", (), "check"),))
    elif mutation == "compiler":

        def fail_plan(*_args: object, **_kwargs: object) -> object:
            raise ValueError("test compilation failure")

        monkeypatch.setattr("hinoki.runtime.crudplan.core._CrudPlan", fail_plan)
    assert select_crud_plan(binding, pipeline)[1] == reason


class OrdinaryAPI(API, path="/ordinary"):
    @http.get("/")
    async def index(self) -> dict[str, str]:
        return {"status": "ok"}


def test_handwritten_route_is_excluded_before_plan_build() -> None:
    pipeline = Pipeline(Hinoki(include=[OrdinaryAPI]))
    assert select_crud_plan(pipeline.bindings[0], pipeline) == (
        None,
        "not_generated_crud_action",
    )

    def list() -> None:
        return None

    binding = SimpleNamespace(route=SimpleNamespace(handler=list, owner=None))
    assert crudplan_module.crud_candidate(cast(Binding, binding)) == (
        None,
        "not_generated_crud_action",
    )


def test_plan_selector_exceptions_keep_startup_on_the_python_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = plan_pipeline()

    def fail_selection(*_args: object) -> tuple[None, str]:
        raise RuntimeError("test selection failure")

    monkeypatch.setattr(servercore_module, "select_crud_plan", fail_selection)
    with caplog.at_level("WARNING", logger="hinoki.server"):
        build_server_core(pipeline)
    assert all(selection is None for selection in pipeline.crud_plan_selections)
    assert set(pipeline.crud_plan_reasons) == {"native_plan_build_failed"}
    assert "native CRUD plan selection failed for GET /plan-items" in caplog.text


def test_native_crud_activation_fails_closed_for_missing_and_failed_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CrudPlanSelection, "snapshot_valid", lambda _self: True)
    database = object.__new__(Database)
    database.engine = cast(core.Database, object())

    for mode, expected in (
        ("missing", "database_missing_after_startup"),
        ("attach", "database_attach_failed"),
        ("arm", "native_plan_arm_failed"),
    ):
        pipeline = plan_pipeline()
        build_server_core(pipeline)
        cast(Any, pipeline).ctx = SimpleNamespace(
            find=lambda _type, mode=mode: None if mode == "missing" else database
        )
        disarmed: list[int] = []

        class CoreStub:
            def _database_attached(self) -> bool:
                return False

            def _attach_database(self, _engine: object, mode: str = mode) -> None:
                if mode == "attach":
                    raise RuntimeError("pool clone rejected")

            def _disarm_crud_plans(self) -> None:
                pass

            def _disarm_crud(self, action: int, disarmed: list[int] = disarmed) -> None:
                disarmed.append(action)

            def _arm_crud(self, _action: int) -> bool:
                return False

        worker = object.__new__(KisoWorker)
        worker.pipeline = pipeline
        worker.core = cast(Any, CoreStub())
        worker._disarmed_crud_actions = set()
        worker._activate_crud_plans()
        worker._disarm_crud_plans("repeated_disarm")

        assert set(filter(None, pipeline.crud_plan_reasons)) == {expected}
        if expected == "native_plan_arm_failed":
            assert len(disarmed) == len(pipeline.crud_plan_selections)


async def test_deferred_request_reuses_buffered_body_once_with_the_same_limit() -> None:
    server = build_server_core(plan_pipeline())
    server.set_ready(True)
    _submission = server.submit("POST", "/plan-items", b"", [])
    [request] = server.drain(1)
    with pytest.raises(RuntimeError, match="completed native body read"):
        request._buffer_body(b"payload")
    assert await request._native_body(7) == b""
    request._buffer_body(b"payload")
    with pytest.raises(RuntimeError, match="completed native body read"):
        request._buffer_body(b"replacement")

    with pytest.raises(core.BodyReadError, match="too_large"):
        await request.body(3)

    _submission = server.submit("POST", "/plan-items", b"", [])
    [request] = server.drain(1)
    assert await request._native_body(7) == b""
    request._buffer_body(b"payload")
    assert await request.body(7) == b"payload"
    with pytest.raises(RuntimeError, match="already taken"):
        await request.body(7)


async def test_native_failure_error_handlers_obey_the_request_timeout() -> None:
    app = Hinoki(
        runtime=RuntimeConfig(request_timeout=0.01),
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///unused-native-error.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)

    async def slow_unique(_error: core.UniqueViolation) -> Conflict:
        await asyncio.sleep(0.1)
        return Conflict("A unique value is already in use.", code="unique_conflict")

    app.errors.handle(core.UniqueViolation)(slow_unique)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    response = await pipeline.handle_native_failure(
        action,
        "request-id",
        ("unique", "unique constraint violated", None, None, "23505"),
    )
    assert response.status == 504


async def test_native_failure_translation_errors_use_the_error_registry() -> None:
    @http.get("/manual-native-failure")
    async def manual() -> dict[str, bool]:
        return {"ok": True}

    pipeline = Pipeline(Hinoki(include=[manual]))
    build_server_core(pipeline)
    response = await pipeline.handle_native_failure(
        0, "request-id", ("not_found", "missing", None, None, None)
    )
    assert response.status == 500


async def test_native_error_handler_timeout_is_translated_as_an_error() -> None:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///unused-native-error.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)

    async def internal_timeout(_error: core.UniqueViolation) -> Conflict:
        raise TimeoutError("handler timeout")

    app.errors.handle(core.UniqueViolation)(internal_timeout)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    response = await pipeline.handle_native_failure(
        action, "request-id", ("unique", "duplicate", None, None, "23505")
    )
    assert response.status == 500


async def test_native_failure_error_translation_timeout_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)

    attempts = 0

    async def interrupted(*_args: Any, **_kwargs: Any) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("error translation timed out")
        return Response(500, b"fallback")

    monkeypatch.setattr("hinoki.runtime.pipeline.error_response", interrupted)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    response = await pipeline.handle_native_failure(
        action, "request-id", ("unique", "duplicate", None, None, "23505")
    )
    assert response.status == 500


async def test_native_database_timeout_returns_gateway_timeout() -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "list"
    )

    response = await pipeline.handle_native_failure(
        action, "request-id", ("timeout", "request timed out", None, None, None)
    )

    assert response.status == 504


async def test_native_retrieve_empty_rows_uses_model_not_found() -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "retrieve"
    )
    selection = pipeline.crud_plan_selections[action]
    assert selection is not None
    with pytest.raises(selection.model.NotFound):
        await pipeline._hydrate_native_rows(action, ([], None))


async def test_native_rows_shape_errors_use_the_error_registry() -> None:
    @http.get("/manual-native-rows")
    async def manual() -> dict[str, bool]:
        return {"ok": True}

    plain = Pipeline(Hinoki(include=[manual]))
    build_server_core(plain)
    no_plan = await plain.handle_native_rows(0, "request-id", ([], None))
    assert no_plan.status == 500

    pipeline = plan_pipeline()
    build_server_core(pipeline)
    list_action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "list"
    )
    no_page = await pipeline.handle_native_rows(list_action, "request-id", ([], None))
    assert no_page.status == 500

    create_action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    wrong_action = await pipeline.handle_native_rows(
        create_action, "request-id", ([], None)
    )
    assert wrong_action.status == 500

    retrieve_action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "retrieve"
    )
    duplicate_rows = [
        {"id": uuid4(), "name": "duplicate", "label": "saved"},
        {"id": uuid4(), "name": "duplicate", "label": "saved"},
    ]
    multiple = await pipeline.handle_native_rows(
        retrieve_action, "request-id", (duplicate_rows, None)
    )
    assert multiple.status == 500

    one = await pipeline.handle_native_rows(
        retrieve_action, "request-id", (duplicate_rows[:1], None)
    )
    assert one.status == 200
    assert core.decode_json(one.body)["name"] == "duplicate"


async def test_native_rows_error_handler_timeout_is_translated() -> None:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///unused-native-error.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)

    async def internal_timeout(_error: RuntimeError) -> Conflict:
        raise TimeoutError("handler timeout")

    app.errors.handle(RuntimeError)(internal_timeout)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    response = await pipeline.handle_native_rows(action, "request-id", ([], None))
    assert response.status == 500


async def test_native_rows_error_translation_timeout_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)

    attempts = 0

    async def interrupted(*_args: Any, **_kwargs: Any) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("error translation timed out")
        return Response(500, b"fallback")

    monkeypatch.setattr("hinoki.runtime.pipeline.error_response", interrupted)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "create"
    )
    response = await pipeline.handle_native_rows(action, "request-id", ([], None))
    assert response.status == 500


async def test_native_rows_timeout_returns_gateway_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki(
        runtime=RuntimeConfig(request_timeout=0.01),
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///unused-native-error.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    action = next(
        index
        for index, selection in enumerate(pipeline.crud_plan_selections)
        if selection is not None and selection.action == "list"
    )

    async def slow_hydration(_action: int, _fallback: Any) -> object:
        await asyncio.sleep(0.1)
        return []

    monkeypatch.setattr(pipeline, "_hydrate_native_rows", slow_hydration)
    response = await pipeline.handle_native_rows(action, "request-id", ([], (1, 20, 0)))
    assert response.status == 504


async def test_worker_attaches_and_releases_the_shared_database_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        PlanItems,
        "response_headers",
        (
            ("x-crud-resource", "plan-items"),
            ("access-control-allow-origin", "https://route.example"),
        ),
        raising=False,
    )
    app = Hinoki(
        settings=AppSettings(
            cors=CorsSettings(origins=("https://client.example",), credentials=True)
        ),
        include=[PlanItems],
        modules=[DatabaseModule(f"sqlite:///{tmp_path / 'worker.db'}")],
    )
    app.errors.handle(core.UniqueViolation)(unique_to_conflict)
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()

    eligible = [
        action
        for action, selection in enumerate(worker.pipeline.crud_plan_selections)
        if selection is not None
    ]
    assert worker.core.ready
    assert worker.core._database_attached()
    assert all(worker.core._crud_armed(action) for action in eligible)
    submission = worker.core.submit(
        "GET",
        "/plan-items",
        b"",
        [(b"origin", b"https://client.example")],
    )
    [request] = worker.core.drain(1)
    assert request.respond_final(
        200, [(b"content-type", b"application/json")], b'{"items":[]}'
    )
    reply = submission.reply()
    assert reply is not None
    assert dict(reply[1])[b"x-crud-resource"] == b"plan-items"
    allow_origins = [
        value for name, value in reply[1] if name == b"access-control-allow-origin"
    ]
    assert allow_origins == [b"https://route.example"]
    assert b"access-control-allow-credentials" not in dict(reply[1])

    failed_submission = worker.core.submit("POST", "/plan-items", b"", [])
    [failed_request] = worker.core.drain(1)
    failed_request._set_native_failure(
        "unique",
        "unique constraint violated",
        "plan_items_name_key",
        "plan_items",
        "23505",
    )
    await worker._serve(failed_request)
    assert failed_request._take_native_failure() is None
    failure_reply = failed_submission.reply()
    assert failure_reply is not None
    assert failure_reply[0] == 409
    assert [
        value
        for name, value in failure_reply[1]
        if name == b"access-control-allow-origin"
    ] == [b"https://route.example"]

    rows_submission = worker.core.submit("GET", "/plan-items", b"", [])
    [rows_request] = worker.core.drain(1)
    row_id = uuid4()
    rows_request._set_native_rows(
        [{"id": row_id, "name": "fallback", "label": "saved"}], (1, 20, 1)
    )
    await worker._serve(rows_request)
    rows_reply = rows_submission.reply()
    assert rows_reply is not None
    assert rows_reply[0] == 200
    decoded = core.decode_json(rows_reply[2])
    assert decoded["items"][0] == {
        "id": str(row_id),
        "name": "fallback",
        "label": "saved",
    }
    assert (decoded["page"], decoded["page_size"], decoded["total"]) == (1, 20, 1)
    assert rows_request._take_native_rows() is None

    missing_submission = worker.core.submit("GET", "/plan-items", b"", [])
    [missing_request] = worker.core.drain(1)
    missing_request._set_native_failure("not_found", "PlanItem not found")
    await worker._serve(missing_request)
    missing_reply = missing_submission.reply()
    assert missing_reply is not None
    assert missing_reply[0] == 404

    await worker.stop()
    assert not worker.core._database_attached()
    assert all(not worker.core._crud_armed(action) for action in eligible)


async def _crud_http_request(
    port: int,
    method: str,
    target: str,
    accept: str | None = None,
    body: bytes = b"",
    content_type: str = "application/json",
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, list[tuple[str, str]], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = "".join(f"{name}: {value}\r\n" for name, value in extra_headers)
    accept_header = f"accept: {accept}\r\n" if accept is not None else ""
    content_type_header = (
        f"content-type: {content_type}\r\n"
        if body or method in {"POST", "PATCH"}
        else ""
    )
    request_head = (
        f"{method} {target} HTTP/1.1\r\n"
        "host: test\r\n"
        "x-request-id: 00000000000000000000000000000001\r\n"
        f"{content_type_header}"
        f"content-length: {len(body)}\r\n"
        f"{accept_header}"
        f"{extra}"
        "\r\n"
    )
    writer.write(request_head.encode() + body)
    await writer.drain()
    header_block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = header_block.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next(
        (int(value) for name, value in headers if name == "content-length"), 0
    )
    body = (
        b""
        if method == "HEAD"
        else await asyncio.wait_for(reader.readexactly(length), 5)
    )
    writer.close()
    await writer.wait_closed()
    return status, headers, body  # type: ignore[return-value]


@pytest.mark.skipif(DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_native_list_retrieve_and_head_match_forced_python_lane() -> None:
    assert DATABASE_URL is not None
    database = await Database.connect(DATABASE_URL, max_size=2, wait=1)
    await database.execute("DROP TABLE IF EXISTS crud_plan_items")
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id uuid PRIMARY KEY, name text NOT NULL, label text NOT NULL)"
    )
    item_id = uuid4()
    await database.execute(
        "INSERT INTO crud_plan_items (id, name, label) VALUES ($1, $2, $3)",
        item_id,
        "native 🌲",
        "saved",
    )
    database.close()

    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[PlanItems],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        eligible = [
            action
            for action, selection in enumerate(worker.pipeline.crud_plan_selections)
            if selection is not None
        ]
        assert eligible
        assert all(worker.core._crud_armed(action) for action in eligible)
        targets = [
            ("GET", "/plan-items?page=1&page_size=20", None),
            ("GET", f"/plan-items/{item_id}", None),
            ("GET", "/plan-items/00000000-0000-0000-0000-000000000000", None),
            ("GET", "/plan-items/not-a-uuid", None),
            ("GET", "/plan-items?page=5&page_size=1", None),
            ("HEAD", "/plan-items?page=1&page_size=20", None),
            ("GET", "/plan-items", "text/plain"),
        ]
        native = [
            await _crud_http_request(worker.address[1], method, target, accept)
            for method, target, accept in targets
        ]
        worker.core._disarm_crud_plans()
        assert all(not worker.core._crud_armed(action) for action in eligible)
        python = [
            await _crud_http_request(worker.address[1], method, target, accept)
            for method, target, accept in targets
        ]
        for actual, expected in zip(native, python, strict=True):
            actual_status, actual_headers, actual_body = actual
            expected_status, expected_headers, expected_body = expected
            stable_actual = [
                (name, value)
                for name, value in actual_headers
                if name not in {"date", "x-request-id"}
            ]
            stable_expected = [
                (name, value)
                for name, value in expected_headers
                if name not in {"date", "x-request-id"}
            ]
            assert (actual_status, stable_actual, actual_body) == (
                expected_status,
                stable_expected,
                expected_body,
            )
    finally:
        await worker.stop()
        cleanup = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await cleanup.execute("DROP TABLE IF EXISTS crud_plan_items")
        cleanup.close()


def require_sql_trace() -> None:
    """Skip without the private trace feature, unless CI requires it."""
    if hasattr(core.Database, "_clear_sql_trace"):
        return
    message = "the private SQL trace feature is not built into this extension"
    if REQUIRE_SQL_TRACE:
        pytest.fail(f"HINOKI_REQUIRE_SQL_TRACE=1 but {message}")
    pytest.skip(message)


def test_required_sql_trace_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "Database", types.SimpleNamespace())
    monkeypatch.setattr(sys.modules[__name__], "REQUIRE_SQL_TRACE", True)
    with pytest.raises(pytest.fail.Exception, match="HINOKI_REQUIRE_SQL_TRACE=1"):
        require_sql_trace()
    monkeypatch.setattr(sys.modules[__name__], "REQUIRE_SQL_TRACE", False)
    with pytest.raises(pytest.skip.Exception):
        require_sql_trace()
    monkeypatch.setattr(
        core, "Database", types.SimpleNamespace(_clear_sql_trace=object())
    )
    require_sql_trace()


async def _crud_write_sequence(
    port: int, engine: Any
) -> tuple[
    list[tuple[int, list[tuple[str, str]], bytes]],
    list[list[tuple[str, str, tuple[Any, ...]]]],
]:
    cases = [
        await _traced_crud_http_request(engine, port, "POST", "/plan-items", body=b"{")
    ]
    cases.append(
        await _traced_crud_http_request(engine, port, "POST", "/plan-items", body=b"{}")
    )
    cases.append(
        await _traced_crud_http_request(
            engine,
            port,
            "POST",
            "/plan-items",
            body=b'{"name":"seed"}',
            content_type="text/plain",
        )
    )
    created, created_trace = await _traced_crud_http_request(
        engine, port, "POST", "/plan-items", body=b'{"name":"seed"}'
    )
    item_id = UUID(core.decode_json(created[2])["id"])
    target = f"/plan-items/{item_id}"
    cases.append((created, created_trace))
    for method, path, body in (
        ("POST", "/plan-items", b'{"name":"seed"}'),
        ("POST", "/plan-items?%FF=1", b'{"name":"query"}'),
        ("DELETE", f"{target}?%FF", b""),
        ("PATCH", target, b"{}"),
        ("PATCH", target, b'{"name":"seed"}'),
        ("PATCH", target, b'{"name":"changed"}'),
        ("DELETE", target, b""),
    ):
        cases.append(
            await _traced_crud_http_request(engine, port, method, path, body=body)
        )
    return [response for response, _ in cases], [trace for _, trace in cases]


async def _traced_crud_http_request(
    engine: Any,
    port: int,
    method: str,
    target: str,
    body: bytes = b"",
    content_type: str = "application/json",
) -> tuple[
    tuple[int, list[tuple[str, str]], bytes],
    list[tuple[str, str, tuple[Any, ...]]],
]:
    engine._clear_sql_trace()
    response = await _crud_http_request(
        port, method, target, body=body, content_type=content_type
    )
    trace = [
        (kind, sql, tuple(_mask_sql_value(value) for value in values))
        for kind, sql, values in engine._take_sql_trace()
    ]
    return response, trace


def _mask_sql_value(value: Any) -> Any:
    return "<uuid>" if isinstance(value, UUID) else value


def _assert_crud_responses_equal(
    actual_results: list[tuple[int, list[tuple[str, str]], bytes]],
    expected_results: list[tuple[int, list[tuple[str, str]], bytes]],
) -> None:
    for actual, expected in zip(actual_results, expected_results, strict=True):
        stable_actual = [
            (name, value)
            for name, value in actual[1]
            if name not in {"date", "x-request-id"}
        ]
        stable_expected = [
            (name, value)
            for name, value in expected[1]
            if name not in {"date", "x-request-id"}
        ]
        actual_body, expected_body = actual[2], expected[2]
        if actual_body and b'"id"' in actual_body:
            actual_data, expected_data = map(
                core.decode_json, (actual_body, expected_body)
            )
            actual_data["id"] = expected_data["id"] = "<generated>"
            actual_body, expected_body = map(
                core.encode_json, (actual_data, expected_data)
            )
        assert (actual[0], stable_actual, actual_body) == (
            expected[0],
            stable_expected,
            expected_body,
        )


def _assert_sql_traces_equal(
    actual_traces: list[list[tuple[str, str, tuple[Any, ...]]]],
    expected_traces: list[list[tuple[str, str, tuple[Any, ...]]]],
) -> None:
    assert actual_traces == expected_traces


@pytest.mark.skipif(DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_native_writes_match_forced_python_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    require_sql_trace()
    database = await Database.connect(DATABASE_URL, max_size=2, wait=1)
    await database.execute("DROP TABLE IF EXISTS crud_plan_items")
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id uuid PRIMARY KEY, name text NOT NULL UNIQUE, label text NOT NULL)"
    )
    database.close()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[PlanItems],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
    await worker.start()
    originals = {
        name: getattr(PlanItems, name) for name in ("store", "change", "remove")
    }
    try:
        for name in originals:
            monkeypatch.setattr(
                PlanItems,
                name,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("native CRUD write called a Python hook")
                ),
            )
        database_context = worker.pipeline.ctx.find(Database)
        assert database_context is not None
        engine = database_context.engine
        native_results, native_traces = await _crud_write_sequence(
            worker.address[1], engine
        )
        assert native_results[4][0] == 409
        worker.core._disarm_crud_plans()
        reset = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await reset.execute("TRUNCATE TABLE crud_plan_items")
        reset.close()
        for name, value in originals.items():
            monkeypatch.setattr(PlanItems, name, value)
        python_results, python_traces = await _crud_write_sequence(
            worker.address[1], engine
        )
        _assert_crud_responses_equal(native_results, python_results)
        _assert_sql_traces_equal(native_traces, python_traces)
    finally:
        await worker.stop()
        cleanup = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await cleanup.execute("DROP TABLE IF EXISTS crud_plan_items")
        cleanup.close()


async def test_native_writes_match_forced_python_lane_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_sql_trace()
    database_url = f"sqlite:///{tmp_path / 'native-writes.db'}"
    database = await Database.connect(database_url, max_size=2, wait=1)
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id UUID PRIMARY KEY, name TEXT NOT NULL UNIQUE, label TEXT NOT NULL)"
    )
    database.close()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[PlanItems],
        modules=[DatabaseModule(database_url)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
    await worker.start()
    originals = {
        name: getattr(PlanItems, name) for name in ("store", "change", "remove")
    }
    try:
        for name in originals:
            monkeypatch.setattr(
                PlanItems,
                name,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("native CRUD write called a Python hook")
                ),
            )
        database_context = worker.pipeline.ctx.find(Database)
        assert database_context is not None
        engine = database_context.engine
        native_results, native_traces = await _crud_write_sequence(
            worker.address[1], engine
        )
        assert native_results[4][0] == 409
        worker.core._disarm_crud_plans()
        reset = await Database.connect(database_url, max_size=1, wait=1)
        await reset.execute("DELETE FROM crud_plan_items")
        reset.close()
        for name, value in originals.items():
            monkeypatch.setattr(PlanItems, name, value)
        python_results, python_traces = await _crud_write_sequence(
            worker.address[1], engine
        )
        _assert_crud_responses_equal(native_results, python_results)
        _assert_sql_traces_equal(native_traces, python_traces)
    finally:
        await worker.stop()


@pytest.mark.skipif(DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_native_write_timeout_rolls_back_before_504() -> None:
    assert DATABASE_URL is not None
    database = await Database.connect(DATABASE_URL, max_size=2, wait=1)
    await database.execute("DROP TABLE IF EXISTS crud_plan_items")
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id uuid PRIMARY KEY, name text NOT NULL UNIQUE, label text NOT NULL)"
    )
    await database.execute(
        "CREATE FUNCTION hinoki_slow_crud_insert() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.5); RETURN NEW; END $$"
    )
    await database.execute(
        "CREATE TRIGGER hinoki_slow_crud_insert BEFORE INSERT ON crud_plan_items "
        "FOR EACH ROW EXECUTE FUNCTION hinoki_slow_crud_insert()"
    )
    database.close()
    app = Hinoki(
        runtime=RuntimeConfig(request_timeout=0.1),
        include=[PlanItems],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    started = asyncio.get_running_loop().time()
    try:
        response = await _crud_http_request(
            worker.address[1], "POST", "/plan-items", body=b'{"name":"timeout"}'
        )
        assert response[0] == 504
        assert asyncio.get_running_loop().time() - started < 0.4
        await asyncio.sleep(0.55)
        inspection = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        remaining = await inspection.fetch("SELECT id FROM crud_plan_items")
        inspection.close()
        assert remaining == []
    finally:
        await worker.stop()
        cleanup = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await cleanup.execute("DROP TABLE IF EXISTS crud_plan_items")
        await cleanup.execute("DROP FUNCTION IF EXISTS hinoki_slow_crud_insert()")
        cleanup.close()


@pytest.mark.skipif(DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_native_write_survives_client_disconnect() -> None:
    assert DATABASE_URL is not None
    database = await Database.connect(DATABASE_URL, max_size=2, wait=1)
    await database.execute("DROP TABLE IF EXISTS crud_plan_items")
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id uuid PRIMARY KEY, name text NOT NULL, label text NOT NULL)"
    )
    database.close()
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule(DATABASE_URL)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", worker.address[1])
        writer.write(
            b"POST /plan-items HTTP/1.1\r\n"
            b"host: test\r\n"
            b"content-type: application/json\r\n"
            b"content-length: 23\r\n\r\n"
            b'{"name":"disconnected"}'
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        inspection = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        deadline = asyncio.get_running_loop().time() + 2
        rows: list[dict[str, Any]] = []
        while not rows and asyncio.get_running_loop().time() < deadline:
            rows = await inspection.fetch(
                "SELECT name FROM crud_plan_items WHERE name = $1", "disconnected"
            )
            if not rows:
                await asyncio.sleep(0.02)
        inspection.close()
        assert rows == [{"name": "disconnected"}]
    finally:
        await worker.stop()
        cleanup = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await cleanup.execute("DROP TABLE IF EXISTS crud_plan_items")
        cleanup.close()


def test_not_ready_requests_cannot_enter_the_native_crud_path() -> None:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///unused-native-error.sqlite3")],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    assert not worker.core.ready
    submission = worker.core.submit("GET", "/plan-items", b"", [])
    reply = submission.early
    assert reply is not None
    assert reply[0] == 503
    assert core.decode_json(reply[2])["error"] == "not_ready"


async def test_startup_mutation_disarms_crud_plans_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate_crud(_: ModuleContext) -> None:
        monkeypatch.setattr(PlanItems, "get_queryset", lambda _self: None)

    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule(f"sqlite:///{tmp_path / 'stale.db'}")],
        on_startup=[mutate_crud],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()

    eligible = [
        action
        for action, selection in enumerate(worker.pipeline.crud_plan_selections)
        if selection is not None
    ]
    assert worker.core.ready
    assert not worker.core._database_attached()
    assert all(not worker.core._crud_armed(action) for action in eligible)
    assert set(worker.pipeline.crud_plan_reasons) == {"native_plan_stale"}

    await worker.stop()


async def test_housekeeping_disarms_a_plan_after_a_snapshot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule(f"sqlite:///{tmp_path / 'tick.db'}")],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    eligible = [
        action
        for action, selection in enumerate(worker.pipeline.crud_plan_selections)
        if selection is not None
    ]
    assert all(worker.core._crud_armed(action) for action in eligible)

    monkeypatch.setattr(PlanItems, "get_queryset", lambda _self: None)
    worker.housekeeping()

    assert all(not worker.core._crud_armed(action) for action in eligible)
    assert set(worker.pipeline.crud_plan_reasons) == {"native_plan_stale"}
    await worker.stop()


async def test_snapshot_exception_fails_closed_before_pool_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_snapshot(_selection: CrudPlanSelection) -> bool:
        raise RuntimeError("snapshot failure")

    monkeypatch.setattr(CrudPlanSelection, "snapshot_valid", fail_snapshot)
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule(f"sqlite:///{tmp_path / 'snapshot-failure.db'}")],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()

    eligible = [
        action
        for action, selection in enumerate(worker.pipeline.crud_plan_selections)
        if selection is not None
    ]
    assert worker.core.ready
    assert not worker.core._database_attached()
    assert all(not worker.core._crud_armed(action) for action in eligible)
    assert set(worker.pipeline.crud_plan_reasons) == {"native_plan_stale"}
    await worker.stop()


async def test_cancelled_drain_waiter_keeps_waiting_for_the_rust_drain() -> None:
    release = asyncio.Event()

    class SlowCore:
        async def _drain_native(self, _timeout_ms: int) -> int:
            await release.wait()
            return 2

    worker_drain = asyncio.create_task(
        _drain_native_uninterruptibly(cast(core.ServerCore, SlowCore()), 100)
    )
    await asyncio.sleep(0)
    worker_drain.cancel()
    await asyncio.sleep(0)
    assert not worker_drain.done()

    release.set()
    assert await worker_drain == (2, True)


def test_schema_fields_outside_the_model_are_ineligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = {**PlanItemIn.specs(), "ghost": PlanItemIn.specs()["name"]}
    monkeypatch.setattr(PlanItemIn, "__specs__", specs)
    pipeline = plan_pipeline()
    assert select_crud_plan(pipeline.bindings[0], pipeline) == (
        None,
        "custom_or_unsupported_schema",
    )


def test_read_schema_constraints_are_ineligible_for_native_crud() -> None:
    class ConstrainedRead(Schema):
        id: UUID
        name: Annotated[str, Field(max_length=3)]
        label: str

    assert not crudplan_module.plain_crud_schema(ConstrainedRead, PlanItem, "read")


def test_read_schema_nullability_must_match_the_model() -> None:
    class NullableModel(Model, table="crud_plan_nullable_items"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        note: str | None

    class NonNullableRead(Schema):
        id: UUID
        note: str

    assert not crudplan_module.plain_crud_schema(NonNullableRead, NullableModel, "read")


def test_route_and_model_metadata_guards_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = SimpleNamespace(
        method="GET", path="/", handler=crudplan_module.ACTION_HANDLERS["list"]
    )
    monkeypatch.setattr(PlanItems, "prefix", "")
    assert crudplan_module.route_matches_action(route, "list", PlanItems)
    assert not crudplan_module.route_matches_action(route, "unknown", PlanItems)
    assert not crudplan_module.route_matches_action(
        SimpleNamespace(method="GET", path="/", handler=object()), "list", PlanItems
    )

    monkeypatch.setattr(PlanItem, "__field_types__", {})
    assert crudplan_module.model_fields(PlanItem) is None


def test_mounted_crud_routes_remain_eligible() -> None:
    app = Hinoki(
        include=[Mount("/api", PlanItems)],
        modules=[DatabaseModule("sqlite:///mounted-crud-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)

    selections = [
        selection
        for selection in pipeline.crud_plan_selections
        if selection is not None
    ]

    assert {selection.action for selection in selections} == {
        "list",
        "retrieve",
        "create",
        "update",
        "delete",
    }


def test_default_metadata_accepts_only_immutable_native_values() -> None:
    assert crudplan_module.encode_default(None) == ("none", "")
    assert crudplan_module.encode_default(True) == ("bool", "true")
    assert crudplan_module.encode_default(-2) == ("int", "-2")
    assert crudplan_module.encode_default(2.5) == ("float", "2.5")
    assert crudplan_module.encode_default("text") == ("str", "text")
    assert crudplan_module.encode_default(math.inf) is None
    assert crudplan_module.encode_default(object()) is None
    assert crudplan_module.schema_default_specs(None) == []


def test_custom_default_factory_is_rejected() -> None:
    factory = lambda: "generated"  # noqa: E731

    class FactoryModel(Model, table="crud_plan_factory_items"):
        id: UUID = field(primary_key=True, default=UUID(int=3))
        name: str = field(default_factory=factory)

    assert (
        crudplan_module.model_field_default(dataclasses.fields(FactoryModel)[1]) is None
    )

    class FactoryInput(Schema):
        id: UUID

    resource = dataclasses.replace(
        PlanItems.resource, model=FactoryModel, create=FactoryInput
    )
    assert crudplan_module.model_default_specs(FactoryModel, resource) is None
    assert (
        crudplan_module.model_default_specs(
            FactoryModel, dataclasses.replace(resource, create=None)
        )
        is None
    )


def test_invalid_database_url_and_page_strategy_are_rejected() -> None:
    assert crudplan_module.dialect_of(DatabaseModule("postgres://[")) is None
    binding = SimpleNamespace(params=[SimpleNamespace(source="page", strategy=None)])
    assert crudplan_module.pagination_limits(cast(Binding, binding), "list") == (
        None,
        0,
    )


def test_default_and_field_metadata_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    original_types = PlanItem.__field_types__
    fields = dict(original_types)
    fields["name"] = dataclasses.replace(fields["name"], kind="unsupported")
    monkeypatch.setattr(PlanItem, "__field_types__", fields)
    assert crudplan_module.model_fields(PlanItem) is None
    monkeypatch.setattr(PlanItem, "__field_types__", {})
    assert crudplan_module.model_fields(PlanItem) is None
    monkeypatch.setattr(PlanItem, "__field_types__", original_types)

    resource = dataclasses.replace(PlanItems.resource, create=None)
    defaults = crudplan_module.model_default_specs(PlanItem, resource)
    assert defaults == [
        ("id", "uuid4", ""),
        ("name", "required", ""),
        ("label", "str", "model-default"),
    ]
    assert (
        crudplan_module.model_default_specs(cast(type[Model], object()), resource)
        is None
    )
    assert crudplan_module.schema_default_is_plain(
        dataclasses.fields(PlanItemPatch)[0], "read"
    )
    monkeypatch.setattr(PlanItem, "__updated_at__", "id")
    assert crudplan_module.write_columns(PlanItem, PlanItems.resource, "update") == (
        "name",
        "label",
        "id",
    )

    class ClockModel(Model, table="crud_plan_clock_items"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        created_at: datetime = field(default_factory=utc_now)

    clock_resource = dataclasses.replace(
        PlanItems.resource, model=ClockModel, create=None
    )
    assert crudplan_module.model_default_specs(ClockModel, clock_resource) == [
        ("id", "uuid4", ""),
        ("created_at", "utc_now", ""),
    ]


def test_invalid_schema_defaults_and_lowering_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidDefaultSchema(Schema):
        id: UUID
        name: str = cast(str, object())
        label: str

    class NonFiniteDefaultSchema(Schema):
        value: float = math.nan

    assert not crudplan_module.plain_crud_schema(InvalidDefaultSchema, PlanItem, "read")
    assert crudplan_module.schema_default_specs(NonFiniteDefaultSchema) is None

    def fail_lowering(_schema: type[Schema]) -> None:
        raise ValueError("test lowering failure")

    monkeypatch.setattr(PlanItemOut, "native_schema", classmethod(fail_lowering))
    assert crudplan_module.schema_plan_args(PlanItems.resource) is None
    assert crudplan_module.parse_ordering("-name") == ("name", True)


def test_compile_selection_returns_lane_c_on_staged_plan_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = plan_pipeline()
    binding = pipeline.bindings[0]
    candidate, _ = crudplan_module.crud_candidate(binding)
    assert candidate is not None
    module = cast(DatabaseModule, pipeline.app.modules[0])

    monkeypatch.setattr(crudplan_module, "pagination_limits", lambda *_: (None, 0))
    assert (
        crudplan_module.compile_selection(candidate, binding, pipeline.app, module)
        is None
    )
    monkeypatch.setattr(crudplan_module, "pagination_limits", lambda *_: (20, 100))
    schema_args = crudplan_module.schema_plan_args(PlanItems.resource)
    assert schema_args is not None
    monkeypatch.setattr(crudplan_module, "schema_plan_args", lambda *_: None)
    assert (
        crudplan_module.compile_selection(candidate, binding, pipeline.app, module)
        is None
    )
    assert not crudplan_module.database_is_plain(
        pipeline.app,
        cast(DatabaseModule, object()),
        PlanItem,
    )
    monkeypatch.setattr(crudplan_module, "schema_plan_args", lambda *_: schema_args)
    monkeypatch.setattr(crudplan_module, "model_fields", lambda *_: None)
    assert (
        crudplan_module.compile_selection(candidate, binding, pipeline.app, module)
        is None
    )


class NoteItem(Model, table="crud_plan_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    note: str | None = None


class Tokens(BearerAuth[str]):
    async def resolve_token(self, token: str) -> Caller[str] | None:
        return Caller(id=token, user=token, permissions=frozenset())


def variant_pipeline(
    read: type[Schema],
    hooks: dict[str, object] | None = None,
    **options: Any,
) -> Pipeline:
    options.setdefault("public_writes", True)
    owner: type[API] = crud(NoteItem, path="/notes", read=read, **options)(
        types.new_class("Variant", (API,), {}, lambda ns: ns.update(hooks or {}))
    )
    return Pipeline(
        Hinoki(
            include=[owner],
            modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
        )
    )


def reasons(pipeline: Pipeline) -> set[str]:
    build_server_core(pipeline)
    return set(pipeline.crud_plan_reasons)


class ConstrainedRead(Schema):
    id: UUID
    name: Annotated[str, Field(max_length=3)]
    note: str | None


class NonNullRead(Schema):
    id: UUID
    name: str
    note: str


class PlainRead(Schema):
    id: UUID
    name: str
    note: str | None


@pytest.mark.parametrize("read", [ConstrainedRead, NonNullRead])
def test_read_schema_constraints_and_nullability_keep_lane_c(
    read: type[Schema],
) -> None:
    assert reasons(variant_pipeline(read)) == {"custom_or_unsupported_schema"}


def test_matching_read_schema_is_eligible() -> None:
    assert reasons(variant_pipeline(PlainRead)) == {"eligible"}


def test_mounted_crud_routes_get_plans() -> None:
    app = Hinoki(
        include=[Mount("/api", PlanItems)],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    assert reasons(pipeline) == {"eligible"}
    assert all(
        item is not None and item.snapshot_valid()
        for item in pipeline.crud_plan_selections
    )


def test_mount_prefix_does_not_hide_a_changed_route_shape() -> None:
    route = SimpleNamespace(
        method="GET", path="/api/other", handler=crudplan_module.ACTION_HANDLERS["list"]
    )
    assert not crudplan_module.route_matches_action(route, "list", PlanItems)


@pytest.mark.parametrize(
    "name",
    [
        "python_list_rows",
        "schema_items",
        "wire_list_rows",
        "current_database",
        "Page",
    ],
)
def test_lane_c_module_globals_are_snapshotted(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selections = [item for item in pipeline.crud_plan_selections if item]
    assert all(item.snapshot_valid() for item in selections)
    monkeypatch.setattr(crud_module, name, lambda *_a, **_k: None)
    assert not all(item.snapshot_valid() for item in selections)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (QuerySet, "__await__"),
        (QuerySet, "__init__"),
        (QuerySet, "compile"),
        (Model, "concrete"),
        (Model, "load_stored"),
        (_CrudConfigType := crud_module._CrudConfig, "values"),
    ],
)
def test_lane_c_class_attributes_are_snapshotted(
    owner: type, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selections = [item for item in pipeline.crud_plan_selections if item]
    monkeypatch.setattr(owner, name, lambda *_a, **_k: None)
    assert not all(item.snapshot_valid() for item in selections)


@pytest.mark.parametrize(
    "mutation", ["json_columns", "errors_type", "find", "translate"]
)
def test_value_and_error_snapshot_blind_spots(
    mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = plan_pipeline()
    build_server_core(pipeline)
    selection = next(item for item in pipeline.crud_plan_selections if item)
    registry = type(pipeline.app.errors)
    if mutation == "json_columns":
        monkeypatch.setattr(PlanItem, "__json_columns__", ("name",))
    elif mutation == "errors_type":
        monkeypatch.setattr(pipeline.app, "errors", type("Custom", (registry,), {})())
    else:
        monkeypatch.setattr(registry, mutation, lambda *_a, **_k: None)
    assert not selection.snapshot_valid()


def custom_queryset(_self: object) -> QuerySet[NoteItem]:
    return QuerySet(NoteItem)


@pytest.mark.parametrize(
    "options",
    [
        {
            "access": CrudAccess(
                list=Access(auth=Tokens(), permissions=[]),
                retrieve=Access(auth=Tokens(), permissions=[]),
            )
        },
        {"access": Access(auth=Tokens(), permissions=[]), "owner": "name"},
        {"access": Access(auth=Tokens(), permissions=[]), "assign": {"name": str}},
        {"hooks": {"get_queryset": custom_queryset}},
        {"hooks": {"to_schema": lambda _self, item: item}},
    ],
    ids=["read_access", "owner", "assign", "get_queryset", "to_schema"],
)
def test_customized_owner_keeps_lane_c(options: dict[str, Any]) -> None:
    assert reasons(variant_pipeline(PlainRead, **options)) <= {
        "custom_route_or_access_policy",
        "custom_owner_type",
    }
    assert "eligible" not in reasons(variant_pipeline(PlainRead, **options))


def test_custom_parser_or_renderer_keeps_lane_c() -> None:
    pipeline = plan_pipeline()
    binding = pipeline.bindings[0]
    route = binding.route
    assert crudplan_module.route_parsing_is_plain(route)
    assert crudplan_module.route_rendering_is_plain(route)
    assert not crudplan_module.route_parsing_is_plain(
        SimpleNamespace(parsers=(object(),))
    )
    assert not crudplan_module.route_rendering_is_plain(
        SimpleNamespace(renderers=(object(),))
    )


def test_model_default_of_another_kind_than_its_column_is_rejected() -> None:
    class Mismatch(Model, table="crud_plan_mismatch"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        count: int = 0

    resource = dataclasses.replace(PlanItems.resource, model=Mismatch, create=None)
    assert crudplan_module.model_default_specs(Mismatch, resource) is not None
    assert crudplan_module.default_fits_column(Mismatch, "count", "int")
    assert not crudplan_module.default_fits_column(Mismatch, "count", "str")
    assert not crudplan_module.default_fits_column(Mismatch, "id", "utc_now")
    assert not crudplan_module.default_fits_column(Mismatch, "count", "none")
    assert not crudplan_module.default_fits_column(Mismatch, "ghost", "int")
    assert crudplan_module.default_fits_column(Mismatch, "count", "required")


@pytest.mark.parametrize("mutation", ["subclass", "defaults"])
def test_custom_error_registry_keeps_lane_c(mutation: str) -> None:
    app = Hinoki(
        include=[PlanItems],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    if mutation == "subclass":
        app.errors = type("Custom", (type(app.errors),), {})()
    else:
        app.errors.defaults.clear()
    pipeline = Pipeline(app)
    assert select_crud_plan(pipeline.bindings[0], pipeline)[1] == (
        "database_methods_or_not_found_handler_customized"
    )


def plan_items_app() -> Hinoki:
    return Hinoki(
        include=[PlanItems, ordinary_health],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )


@http.get("/ordinary")
def ordinary_health() -> dict[str, str]:
    return {"status": "ok"}


def test_routes_command_reports_the_lane_of_each_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = "tests.crudplantest:plan_items_app"
    assert main(["routes", target, "--format", "json"]) == 0
    found = {
        (item["method"], item["path"]): (item["lane"], item["lane_reason"])
        for item in json.loads(capsys.readouterr().out)
    }
    assert found[("GET", "/plan-items")] == ("native", "eligible")
    assert found[("DELETE", "/plan-items/{item_id}")] == ("native", "eligible")
    assert found[("GET", "/ordinary")] == ("python", "not_generated_crud_action")

    assert main(["routes", target]) == 0
    assert "[" not in capsys.readouterr().out.split("/ordinary")[0].splitlines()[-1]
    assert main(["routes", target, "--lanes", "--path", "/plan-items"]) == 0
    assert "[native: eligible]" in capsys.readouterr().out


class ClaimsTokens(ClaimsJWTAuth):
    keys = (JWTKey(None, "HS256", b"k" * 32),)


CLAIMS_KEY = ClaimsTokens.keys[0].spec()


def claims_token(claims: dict[str, Any], key: bytes = b"k" * 32) -> str:
    return core.jwt_encode(core.encode_json(claims), (None, "HS256", key, False))


@crud(
    PlanItem,
    path="/claims-items",
    read=PlanItemOut,
    create=PlanItemIn,
    update=PlanItemPatch,
    delete=True,
    access=CrudAccess(
        default=Access(ClaimsTokens(), permissions=["items:write"]),
        list=Access(ClaimsTokens(), permissions=["items:read"]),
        retrieve=Access(ClaimsTokens(), permissions=["items:read"]),
    ),
)
class ClaimsItems(API):
    pass


def claims_app(url: str) -> Hinoki:
    return Hinoki(include=[ClaimsItems], modules=[DatabaseModule(url)])


def test_claims_auth_routes_are_selected_for_the_native_lane() -> None:
    pipeline = Pipeline(claims_app("sqlite:///crud-plan-unused.sqlite3"))
    build_server_core(pipeline)
    assert set(pipeline.crud_plan_reasons) == {"eligible"}
    assert all(item is not None for item in pipeline.crud_plan_selections)


@pytest.mark.parametrize(
    "change",
    ["resolve_claims", "now", "authenticate", "resolve_token"],
)
def test_overriding_the_claims_provider_keeps_the_python_lane(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pipeline = Pipeline(claims_app("sqlite:///crud-plan-unused.sqlite3"))
    build_server_core(pipeline)
    selections = [item for item in pipeline.crud_plan_selections if item is not None]
    assert all(item.snapshot_valid() for item in selections)
    monkeypatch.setattr(ClaimsTokens, change, lambda *_a, **_k: None)
    assert not any(item.snapshot_valid() for item in selections)


def test_changing_keys_or_permissions_disarms_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = Pipeline(claims_app("sqlite:///crud-plan-unused.sqlite3"))
    build_server_core(pipeline)
    selections = [item for item in pipeline.crud_plan_selections if item is not None]
    monkeypatch.setattr(ClaimsTokens, "keys", (JWTKey(None, "HS256", b"r" * 32),))
    assert not any(item.snapshot_valid() for item in selections)
    monkeypatch.undo()
    monkeypatch.setattr(ClaimsTokens, "issuer", "https://issuer.example")
    assert not any(item.snapshot_valid() for item in selections)


def test_a_custom_provider_or_instance_override_keeps_the_python_lane() -> None:
    class Custom(ClaimsJWTAuth):
        keys = (JWTKey(None, "HS256", b"k" * 32),)

        def resolve_claims(self, claims: dict[str, Any]) -> None:
            return None

    def declared(path: str, tokens: ClaimsJWTAuth) -> type[API]:
        return crud(
            PlanItem,
            path=path,
            read=PlanItemOut,
            access=CrudAccess(
                list=Access(tokens, permissions=[]),
                retrieve=Access(tokens, permissions=[]),
            ),
        )(types.new_class("Declared", (API,)))

    class Elsewhere(ClaimsTokens):
        header = "x-token"

    tokens = ClaimsTokens()
    tokens.now = lambda: 0  # type: ignore[method-assign]
    Variant = declared("/variant", Custom())
    Moved = declared("/moved", Elsewhere())
    Patched = declared("/patched", tokens)

    for owner in (Variant, Patched, Moved):
        pipeline = Pipeline(
            Hinoki(
                include=[owner],
                modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
            )
        )
        build_server_core(pipeline)
        assert set(pipeline.crud_plan_reasons) == {"custom_route_or_access_policy"}


AUTH_HEADERS: list[tuple[str, tuple[tuple[str, str], ...]]] = []


def auth_cases() -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    now = int(utc_now().timestamp())
    good = {"sub": "u1", "exp": now + 600, "scopes": ["items:read", "items:write"]}

    def bearer(token: str) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {token}"),)

    return [
        ("none", ()),
        ("garbage", bearer("not.a.token")),
        ("basic", (("authorization", "Basic abc"),)),
        ("two headers", (*bearer(claims_token(good)), *bearer(claims_token(good)))),
        ("wrong key", bearer(claims_token(good, key=b"x" * 32))),
        ("expired", bearer(claims_token({**good, "exp": now - 5}))),
        ("no sub", bearer(claims_token({k: v for k, v in good.items() if k != "sub"}))),
        ("empty sub", bearer(claims_token({**good, "sub": ""}))),
        ("scopes not a list", bearer(claims_token({**good, "scopes": "items:read"}))),
        (
            "no scopes",
            bearer(claims_token({k: v for k, v in good.items() if k != "scopes"})),
        ),
        ("read only", bearer(claims_token({**good, "scopes": ["items:read"]}))),
        ("write only", bearer(claims_token({**good, "scopes": ["items:write"]}))),
        (
            "extra junk scopes",
            bearer(
                claims_token({**good, "scopes": [1, None, "items:read", "items:write"]})
            ),
        ),
        ("valid", bearer(claims_token(good))),
        ("lowercase scheme", (("authorization", f"bearer {claims_token(good)}"),)),
        ("padded", (("authorization", f"Bearer  {claims_token(good)} "),)),
    ]


@pytest.mark.skipif(DATABASE_URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_native_claims_auth_answers_like_the_python_lane() -> None:
    assert DATABASE_URL is not None
    database = await Database.connect(DATABASE_URL, max_size=2, wait=1)
    await database.execute("DROP TABLE IF EXISTS crud_plan_items")
    await database.execute(
        "CREATE TABLE crud_plan_items ("
        "id uuid PRIMARY KEY, name text NOT NULL, label text NOT NULL)"
    )
    item_id = uuid4()
    await database.execute(
        "INSERT INTO crud_plan_items (id, name, label) VALUES ($1, $2, $3)",
        item_id,
        "seed",
        "saved",
    )
    database.close()
    worker = KisoWorker(claims_app(DATABASE_URL), "127.0.0.1", 0)
    await worker.start()
    try:
        port = worker.address[1]
        eligible = [
            action
            for action, item in enumerate(worker.pipeline.crud_plan_selections)
            if item is not None
        ]
        assert len(eligible) == 5
        assert all(worker.core._crud_armed(action) for action in eligible)
        served: list[object] = []
        serve = worker._serve

        async def counting(request: Any) -> Any:
            served.append(request)
            return await serve(request)

        worker._serve = counting  # type: ignore[method-assign]
        reads = [
            ("GET", "/claims-items?page=1&page_size=20", b""),
            ("GET", f"/claims-items/{item_id}", b""),
        ]
        results: dict[str, list[tuple[Any, int]]] = {}
        for lane in ("native", "python"):
            if lane == "python":
                worker.core._disarm_crud_plans()
            results[lane] = []
            for label, headers in auth_cases():
                for method, target, _body in reads:
                    served.clear()
                    status, response_headers, content = await _crud_http_request(
                        port, method, target, extra_headers=headers
                    )
                    stable = [
                        (n, v)
                        for n, v in response_headers
                        if n not in {"date", "x-request-id"}
                    ]
                    results[lane].append(
                        (
                            (
                                label,
                                method,
                                status,
                                stable,
                                re.sub(
                                    rb'"request_id":"[0-9a-f]+"',
                                    b'"request_id":""',
                                    content,
                                ),
                            ),
                            len(served),
                        )
                    )
        assert [item[0] for item in results["native"]] == [
            item[0] for item in results["python"]
        ]
        statuses = {(case[0], case[1]): case[2] for case, _calls in results["native"]}
        assert statuses[("valid", "GET")] == 200
        assert statuses[("none", "GET")] == 401
        assert statuses[("read only", "GET")] == 200
        assert statuses[("write only", "GET")] == 403
    finally:
        await worker.stop()
        cleanup = await Database.connect(DATABASE_URL, max_size=1, wait=1)
        await cleanup.execute("DROP TABLE IF EXISTS crud_plan_items")
        cleanup.close()


class OwnedNote(Model, table="crud_owned_notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: str
    title: str


class OwnedNoteOut(Schema):
    id: UUID
    owner_id: str
    title: str


class OwnedNoteIn(Schema):
    title: str


class OwnedNotePatch(Schema):
    title: str | Unset = UNSET


OwnedNotes = crud(
    OwnedNote,
    path="/owned-notes",
    read=OwnedNoteOut,
    create=OwnedNoteIn,
    update=OwnedNotePatch,
    delete=True,
    access=CrudAccess(
        default=Access(ClaimsTokens(), permissions=["notes:write"]),
        list=Access(ClaimsTokens(), permissions=["notes:read"]),
        retrieve=Access(ClaimsTokens(), permissions=["notes:read"]),
    ),
    owner="owner_id",
    identity_key=caller_id,
).api()


def owned_pipeline(url: str = "sqlite:///crud-plan-unused.sqlite3") -> Pipeline:
    pipeline = Pipeline(Hinoki(include=[OwnedNotes], modules=[DatabaseModule(url)]))
    build_server_core(pipeline)
    return pipeline


def test_owner_scoped_routes_are_native_only_for_the_caller_id_source() -> None:
    assert set(owned_pipeline().crud_plan_reasons) == {"eligible"}


def test_owner_scope_is_native_on_every_supported_dialect() -> None:
    for url in ("mysql://u:p@localhost/db", "mariadb://u:p@localhost/db"):
        assert set(owned_pipeline(url).crud_plan_reasons) == {"eligible"}


def test_owned_update_read_back_repeats_the_owner_filter() -> None:
    pipeline = owned_pipeline()
    plan = next(
        item.plan
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "update"
    )
    templates = {(dialect, kind): sql for dialect, kind, sql in plan.templates()}

    for dialect in ("mysql", "mariadb"):
        scoped = QuerySet(OwnedNote).filter(id=UUID(int=1), owner_id="o")
        assert (
            templates[dialect, "update_read_back"]
            == scoped.compile(dialect=dialect)[0] + " FOR UPDATE"
        )
        assert "`owner_id` = ?" not in templates[dialect, "write_read_back"]


@pytest.mark.parametrize(
    "attributes",
    [{"identity_key": lambda user: user.id}],
    ids=["custom_identity"],
)
def test_other_owner_declarations_keep_the_python_lane(
    attributes: dict[str, Any],
) -> None:
    options: dict[str, Any] = {
        "create": OwnedNoteIn,
        "update": OwnedNotePatch,
        "delete": True,
        "access": Access(ClaimsTokens(), permissions=["notes:write"]),
        "owner": "owner_id",
        "identity_key": caller_id,
    }
    options.update(attributes)
    variant: type[API] = crud(
        OwnedNote, path="/variant-notes", read=OwnedNoteOut, **options
    )(types.new_class("Variant", (API,)))
    pipeline = Pipeline(
        Hinoki(
            include=[variant],
            modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
        )
    )
    build_server_core(pipeline)
    writes = [
        reason
        for reason, selection in zip(
            pipeline.crud_plan_reasons, pipeline.crud_plan_selections, strict=True
        )
        if selection is None
    ]
    assert len(writes) >= 3


def test_caller_id_owner_comes_from_the_caller_not_the_user() -> None:
    resource = vars(OwnedNotes)["resource"]
    principal = SimpleNamespace(id="u1", user=None)
    assert resource.owner_of(principal) == "u1"


def owned_headers(subject: str, scopes: list[str]) -> tuple[tuple[str, str], ...]:
    now = int(utc_now().timestamp())
    token = claims_token({"sub": subject, "exp": now + 600, "scopes": scopes})
    return (("authorization", f"Bearer {token}"),)


ALL_SCOPES = ["notes:read", "notes:write"]


async def owned_scenario(  # noqa: PLR0915 - one linear request script
    url: str, *, native: bool
) -> tuple[list[object], list[object], int]:
    worker = KisoWorker(
        Hinoki(include=[OwnedNotes], modules=[DatabaseModule(url)]), "127.0.0.1", 0
    )
    await worker.start()
    python_requests = 0
    try:
        port = worker.address[1]
        if not native:
            worker.core._disarm_crud_plans()
        serve = worker._serve

        async def counting(request: Any) -> Any:
            nonlocal python_requests
            python_requests += 1
            return await serve(request)

        worker._serve = counting  # type: ignore[method-assign]
        ids: dict[str, str] = {}
        log: list[object] = []

        async def call(
            label: str,
            method: str,
            target: str,
            headers: tuple[tuple[str, str], ...],
            body: bytes = b"",
            remember: str | None = None,
        ) -> None:
            for name, value in ids.items():
                target = target.replace(f"<{name}>", value)
            status, response_headers, content = await _crud_http_request(
                port, method, target, body=body, extra_headers=headers
            )
            if remember is not None:
                ids[remember] = json.loads(content)["id"]
            text = re.sub(rb'"request_id":"[0-9a-f]+"', b'"request_id":""', content)
            for name, value in ids.items():
                text = text.replace(value.encode(), f"<{name}>".encode())
            if label.startswith("list"):
                listed = json.loads(content)
                listed["items"].sort(key=lambda item: item["title"])
                text = json.dumps(listed).encode()
                for name, value in ids.items():
                    text = text.replace(value.encode(), f"<{name}>".encode())
            stable = [
                (n, v) for n, v in response_headers if n not in {"date", "x-request-id"}
            ]
            log.append((label, status, stable, text))

        alice = owned_headers("alice", ALL_SCOPES)
        bob = owned_headers("bob", ALL_SCOPES)
        await call(
            "alice create",
            "POST",
            "/owned-notes",
            alice,
            b'{"title":"a1"}',
            "alice-note",
        )
        await call(
            "bob create", "POST", "/owned-notes", bob, b'{"title":"b1"}', "bob-note"
        )
        await call(
            "spoofed owner",
            "POST",
            "/owned-notes",
            alice,
            b'{"title":"x","owner_id":"bob"}',
        )
        await call("anonymous create", "POST", "/owned-notes", (), b'{"title":"x"}')
        await call(
            "read-only create",
            "POST",
            "/owned-notes",
            owned_headers("alice", ["notes:read"]),
            b'{"title":"x"}',
        )
        shout = owned_headers("ALICE", ALL_SCOPES)
        await call(
            "case-variant update",
            "PATCH",
            "/owned-notes/<alice-note>",
            shout,
            b'{"title":"case"}',
        )
        await call("case-variant delete", "DELETE", "/owned-notes/<alice-note>", shout)
        await call(
            "own update", "PATCH", "/owned-notes/<alice-note>", alice, b'{"title":"a2"}'
        )
        await call(
            "foreign update",
            "PATCH",
            "/owned-notes/<alice-note>",
            bob,
            b'{"title":"hijack"}',
        )
        await call("foreign delete", "DELETE", "/owned-notes/<alice-note>", bob)
        await call(
            "missing update",
            "PATCH",
            f"/owned-notes/{uuid4()}",
            alice,
            b'{"title":"x"}',
        )
        await call("empty update", "PATCH", "/owned-notes/<bob-note>", alice, b"{}")
        await call("list sees everyone", "GET", "/owned-notes", alice)
        await call("own delete", "DELETE", "/owned-notes/<alice-note>", alice)
        await call("repeat delete", "DELETE", "/owned-notes/<alice-note>", alice)
        counted = python_requests
    finally:
        await worker.stop()
    database = await Database.connect(url, max_size=1, wait=1)
    rows = await database.fetch(
        "SELECT owner_id, title FROM crud_owned_notes ORDER BY title"
    )
    database.close()
    return log, [tuple(row.values()) for row in rows], counted


OWNED_DATABASES = (
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "mysql",
        marks=pytest.mark.skipif(
            "HINOKI_TEST_MYSQL_URL" not in os.environ,
            reason="HINOKI_TEST_MYSQL_URL is not set",
        ),
        id="mysql",
    ),
    pytest.param(
        "mariadb",
        marks=pytest.mark.skipif(
            "HINOKI_TEST_MARIADB_URL" not in os.environ,
            reason="HINOKI_TEST_MARIADB_URL is not set",
        ),
        id="mariadb",
    ),
)


@pytest.mark.parametrize("backend", OWNED_DATABASES)
async def test_native_owner_scope_answers_like_the_python_lane(
    backend: str, tmp_path: Path
) -> None:
    results = []
    for native in (True, False):
        if backend == "sqlite":
            url = f"sqlite:///{tmp_path / f'owned-{native}.db'}"
        else:
            url = os.environ[f"HINOKI_TEST_{backend.upper()}_URL"]
        database = await Database.connect(url, max_size=1, wait=1)
        await database.execute("DROP TABLE IF EXISTS crud_owned_notes")
        await database.execute(
            create_table_sql("crud_owned_notes", table_state(OwnedNote), backend)
        )
        database.close()
        results.append(await owned_scenario(url, native=native))
    (
        (native_log, native_rows, native_python),
        (python_log, python_rows, python_python),
    ) = results
    assert native_log == python_log
    assert native_rows == python_rows
    assert python_python == len(python_log)
    statuses = {entry[0]: entry[1] for entry in native_log}  # type: ignore[index]
    assert statuses["alice create"] == 201
    assert statuses["foreign update"] == 403
    assert statuses["foreign delete"] == 403
    assert statuses["case-variant update"] == 403
    assert statuses["case-variant delete"] == 403
    assert statuses["missing update"] == 404
    assert statuses["own delete"] == 204
    assert statuses["repeat delete"] == 404
    assert statuses["anonymous create"] == 401
    assert statuses["read-only create"] == 403
    assert native_rows == [("bob", "b1")] or ("bob", "b1") in native_rows
    # Only the requests Rust declines (spoofed owner is a validation error it
    # may answer itself) reach Python; the owner-scoped writes must not.
    assert native_python < len(native_log)


def test_a_route_with_its_own_access_is_not_a_plain_crud_route() -> None:
    declared = PlanItems.resource.access_for("list")
    assert crudplan_module.access_is_plain(
        SimpleNamespace(access=declared), PlanItems.resource, "list"
    )
    replaced = SimpleNamespace(access=object())
    assert not crudplan_module.access_is_plain(replaced, PlanItems.resource, "list")


async def _http_get(port: int, target: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            f"GET {target} HTTP/1.1\r\nhost: test\r\n"
            "x-request-id: 00000000000000000000000000000001\r\n"
            "content-length: 0\r\n\r\n"
        ).encode("latin-1")
    )
    await writer.drain()
    block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    lines = block.decode("latin-1").split("\r\n")
    pairs = [tuple(line.split(": ", 1)) for line in lines[1:] if line]
    length = next((int(v) for n, v in pairs if n == "content-length"), 0)
    body = await reader.readexactly(length)
    writer.close()
    await writer.wait_closed()
    return int(lines[0].split()[1]), body


def test_filtered_crud_list_eligibility_and_hooks() -> None:
    class FilteredItem(Model, table="crud_filtered_items"):
        id: int = field(primary_key=True)
        name: str
        status: str
        amount: int
        rate: float
        note: str | None = None

    class FilteredItemOut(Schema):
        id: int
        name: str
        status: str
        amount: int
        rate: float
        note: str | None

    class ItemFilters(FilterSet):
        status: In[Literal["open", "paid"]]
        amount: Range[int]
        rate: Eq[float]
        note: Eq[str | None]
        search = Search("name", "note")
        ordering = ("amount", "name", "rate")

    plain_api = crud(
        FilteredItem,
        path="/items",
        read=FilteredItemOut,
        filters=ItemFilters,
    ).api()

    @crud(FilteredItem, path="/hooked", read=FilteredItemOut, filters=ItemFilters)
    class HookedApi(API):
        def filter_queryset(
            self,
            query: QuerySet[FilteredItem],
            params: ItemFilters,
            principal: Any,
        ) -> QuerySet[FilteredItem]:
            return query

    class CustomFilters(ItemFilters):
        def apply(self, query: QuerySet[Any]) -> QuerySet[Any]:
            return super().apply(query)

    class CustomAssignFilters(ItemFilters):
        def assign(
            self,
            name: str,
            eq: object,
            many: list[object] | None,
            low: object,
            high: object,
        ) -> None:
            super().assign(name, eq, many, low, high)

    custom_filter_api = crud(
        FilteredItem,
        path="/custom-filters",
        read=FilteredItemOut,
        filters=CustomFilters,
    ).api()
    custom_assign_api = crud(
        FilteredItem,
        path="/custom-assign",
        read=FilteredItemOut,
        filters=CustomAssignFilters,
    ).api()
    cursor_api = crud(
        FilteredItem,
        path="/cursor",
        read=FilteredItemOut,
        filters=ItemFilters,
        pagination=CursorPagination(secret="s" * 32),
    ).api()

    url = "sqlite:///crud-filtered-unused.sqlite3"
    plain_pipeline = Pipeline(
        Hinoki(include=[plain_api], modules=[DatabaseModule(url)])
    )
    list_binding = plain_pipeline.bindings[0]
    selection, reason = select_crud_plan(list_binding, plain_pipeline)
    assert reason == "eligible"
    assert selection is not None
    assert selection.snapshot_valid()

    # Removing the compiled FilterSet plan must fail closed (False), not raise.
    saved_plan = ItemFilters.plan
    delattr(ItemFilters, "plan")
    try:
        assert not selection.snapshot_valid()
    finally:
        ItemFilters.plan = saved_plan
    assert selection.snapshot_valid()

    for api, expected in (
        (HookedApi, "custom_route_or_access_policy"),
        (custom_filter_api, "custom_route_or_access_policy"),
        (custom_assign_api, "custom_route_or_access_policy"),
        (cursor_api, "generated_route_shape_changed"),
    ):
        pipe = Pipeline(Hinoki(include=[api], modules=[DatabaseModule(url)]))
        sel, why = select_crud_plan(pipe.bindings[0], pipe)
        assert sel is None
        assert why == expected


async def test_native_filtered_list_matches_python_lane_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Invoice(Model, table="crud_native_filter_invoices"):
        id: int = field(primary_key=True)
        reference: str
        status: str
        amount: int
        rate: float
        note: str | None = None

    class InvoiceOut(Schema):
        id: int
        reference: str
        status: str
        amount: int
        rate: float
        note: str | None

    class InvoiceFilters(FilterSet):
        status: In[Literal["open", "paid", "void"]]
        amount: Range[int]
        rate: Eq[float]
        note: Eq[str | None]
        search = Search("reference", "note")
        ordering = ("amount", "reference", "rate")

    invoices_api = crud(
        Invoice,
        path="/invoices",
        read=InvoiceOut,
        filters=InvoiceFilters,
    ).api()

    database_url = f"sqlite:///{tmp_path / 'native-filters.db'}"
    database = await Database.connect(database_url, max_size=2, wait=1)
    await database.execute(
        create_table_sql("crud_native_filter_invoices", table_state(Invoice), "sqlite")
    )
    rows = [
        (1, "INV-100", "open", 50, 10.5, None),
        (2, "INV-200", "paid", 50, 20.0, "50% discount"),
        (3, "INV-300", "open", 120, 10.5, "rush"),
        (4, "INV-400", "void", 10, 5.0, None),
    ]
    for row in rows:
        await database.execute(
            "INSERT INTO crud_native_filter_invoices "
            "(id, reference, status, amount, rate, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            *row,
        )
    database.close()

    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[invoices_api],
        modules=[DatabaseModule(database_url)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
    await worker.start()

    valid_queries = [
        "/invoices",
        "/invoices?status=open,paid&ordering=amount,-reference",
        "/invoices?amount.from=50&amount.to=120&ordering=amount",
        "/invoices?note.null=true",
        "/invoices?note.null=false&search=50%25",
        "/invoices?rate=10.5&page=1&page_size=1",
    ]
    error_queries = [
        "/invoices?unknown=1",
        "/invoices?status=open&status=paid",
        "/invoices?amount.from=abc",
        "/invoices?ordering=unknown",
    ]
    try:
        original_parse = InvoiceFilters.parse
        monkeypatch.setattr(
            InvoiceFilters,
            "parse",
            classmethod(
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    AssertionError("native filtered list called Python FilterSet.parse")
                )
            ),
        )
        native_valid = [await _http_get(worker.address[1], q) for q in valid_queries]

        # Restore parse for 422 fallback queries (expected to defer to Python).
        monkeypatch.setattr(InvoiceFilters, "parse", original_parse)
        native_errors = [await _http_get(worker.address[1], q) for q in error_queries]

        worker.core._disarm_crud_plans()
        python_valid = [await _http_get(worker.address[1], q) for q in valid_queries]
        python_errors = [await _http_get(worker.address[1], q) for q in error_queries]

        assert native_valid == python_valid
        assert all(status == 200 for status, _ in native_valid)
        assert native_errors == python_errors
        assert all(status == 422 for status, _ in native_errors)
    finally:
        await worker.stop()


class Tier(enum.Enum):
    BASIC = "basic"
    GOLD = "gold"


class RichPlanItem(UUIDModel, SoftDeleteModel, table="crud_rich_items"):
    id: UUID = uuid_pk(version=7)
    name: str
    share: str = urlsafe_token(nbytes=12)
    price: Decimal = Decimal("0.50")
    due: date | None = None


class RichPlanItemOut(Schema):
    id: UUID
    name: str
    share: str
    price: Decimal
    due: date | None


class RichPlanItemIn(Schema):
    name: str
    price: Decimal = Decimal("1.25")
    due: date | None = None


class RichPlanItemPatch(Schema):
    name: str | Unset = UNSET
    price: Decimal | Unset = UNSET
    due: date | Unset | None = UNSET


RichPlanItems = crud(
    RichPlanItem,
    path="/rich-items",
    read=RichPlanItemOut,
    create=RichPlanItemIn,
    update=RichPlanItemPatch,
    delete=True,
    public_writes=True,
).api()


def test_lifecycle_columns_compile_native_crud_plans() -> None:
    app = Hinoki(
        include=[RichPlanItems],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    selected = [item for item in pipeline.crud_plan_selections if item is not None]
    assert [item.action for item in selected] == [
        "list",
        "retrieve",
        "create",
        "update",
        "delete",
    ], pipeline.crud_plan_reasons


@pytest.mark.parametrize("attribute", ["__soft_delete__", "__soft_delete_clock__"])
def test_soft_delete_snapshot_invalidates_selection(
    attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Hinoki(
        include=[RichPlanItems],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    selection = next(
        item
        for item in pipeline.crud_plan_selections
        if item is not None and item.action == "delete"
    )
    assert selection.snapshot_valid()
    monkeypatch.setattr(RichPlanItem, attribute, lambda: None)
    assert not selection.snapshot_valid()


class TierPlanItem(Model, table="crud_tier_items"):
    id: int = field(primary_key=True)
    tier: Tier = Tier.BASIC


class TierPlanItemOut(Schema):
    id: int
    tier: Tier


def test_enum_columns_stay_on_python_crud_lane() -> None:
    app = Hinoki(
        include=[crud(TierPlanItem, path="/tiers", read=TierPlanItemOut).api()],
        modules=[DatabaseModule("sqlite:///crud-plan-unused.sqlite3")],
    )
    pipeline = Pipeline(app)
    build_server_core(pipeline)
    assert not any(pipeline.crud_plan_selections)


async def _rich_crud_sequence(
    port: int, engine: Any
) -> tuple[
    list[tuple[int, list[tuple[str, str]], bytes]],
    list[list[tuple[str, str, tuple[Any, ...]]]],
]:
    async def call(
        method: str, target: str, body: bytes = b""
    ) -> tuple[
        tuple[int, list[tuple[str, str]], bytes],
        list[tuple[str, str, tuple[Any, ...]]],
    ]:
        response, trace = await _traced_crud_http_request(
            engine, port, method, target, body=body
        )
        masked = [
            (kind, sql, tuple(_mask_rich_value(value) for value in values))
            for kind, sql, values in trace
        ]
        return response, masked

    cases = [await call("POST", "/rich-items", b'{"name":"plain"}')]
    created, trace = await call(
        "POST",
        "/rich-items",
        b'{"name":"dated","price":"12.50","due":"2026-02-28"}',
    )
    cases.append((created, trace))
    target = f"/rich-items/{core.decode_json(created[2])['id']}"
    for method, path, body in (
        ("GET", "/rich-items", b""),
        ("GET", target, b""),
        ("PATCH", target, b'{"price":"7.10","due":null}'),
        ("DELETE", target, b""),
        ("GET", target, b""),
        ("PATCH", target, b'{"name":"gone"}'),
        ("DELETE", target, b""),
        ("GET", "/rich-items?page_size=1", b""),
    ):
        cases.append(await call(method, path, body))
    return [_mask_rich_response(item) for item, _ in cases], [
        trace for _, trace in cases
    ]


def _mask_rich_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return "<uuid>"
    if isinstance(value, datetime):
        return "<now>"
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16}", value):
        return "<token>"
    return value


def _mask_rich_response(
    response: tuple[int, list[tuple[str, str]], bytes],
) -> tuple[int, list[tuple[str, str]], bytes]:
    status, headers, body = response
    # The Date header moves with the clock, so two runs a second apart differ.
    headers = [(name, value) for name, value in headers if name != "date"]
    if not body.startswith(b"{"):
        return status, headers, body
    data = core.decode_json(body)
    for item in data.get("items", [data]):
        for name in ("id", "share"):
            if name in item:
                item[name] = f"<{name}>"
    return status, headers, core.encode_json(data)


async def test_lifecycle_columns_match_forced_python_lane_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_sql_trace()
    database_url = f"sqlite:///{tmp_path / 'native-rich.db'}"
    database = await Database.connect(database_url, max_size=2, wait=1)
    await database.execute(
        "CREATE TABLE crud_rich_items (deleted_at TEXT, id UUID PRIMARY KEY, "
        "name TEXT NOT NULL, share TEXT NOT NULL UNIQUE, price TEXT NOT NULL, "
        "due TEXT)"
    )
    database.close()
    app = Hinoki(
        settings=AppSettings(
            server=ServerSettings(trusted_proxies=("127.0.0.1",), trust_request_id=True)
        ),
        include=[RichPlanItems],
        modules=[DatabaseModule(database_url)],
    )
    worker = KisoWorker(app, "127.0.0.1", 0)
    monkeypatch.setattr(worker, "_check_crud_snapshots", lambda: None)
    await worker.start()
    originals = {
        name: getattr(RichPlanItems, name) for name in ("store", "change", "remove")
    }
    try:
        for name in originals:
            monkeypatch.setattr(
                RichPlanItems,
                name,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("native CRUD write called a Python hook")
                ),
            )
        database_context = worker.pipeline.ctx.find(Database)
        assert database_context is not None
        engine = database_context.engine
        native_results, native_traces = await _rich_crud_sequence(
            worker.address[1], engine
        )
        worker.core._disarm_crud_plans()
        reset = await Database.connect(database_url, max_size=1, wait=1)
        await reset.execute("DELETE FROM crud_rich_items")
        reset.close()
        for name, value in originals.items():
            monkeypatch.setattr(RichPlanItems, name, value)
        python_results, python_traces = await _rich_crud_sequence(
            worker.address[1], engine
        )
        assert native_results == python_results
        _assert_sql_traces_equal(native_traces, python_traces)
    finally:
        await worker.stop()
