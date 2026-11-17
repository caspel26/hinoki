"""Startup compilation for the private native CRUD plans."""

import asyncio
import dataclasses
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    CrudAPI,
    Hinoki,
    RuntimeConfig,
    Schema,
    SchemaExtra,
    Unset,
    core,
    http,
)
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Conflict
from hinoki.fields import UNSET
from hinoki.modules import ModuleContext
from hinoki.orm import Model, QuerySet, field
from hinoki.orm.queryset import write_statement
from hinoki.runtime import crudplan as crudplan_module
from hinoki.runtime import servercore as servercore_module
from hinoki.runtime.binding import Binding
from hinoki.runtime.crudplan import CrudPlanSelection, select_crud_plan
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.responses import Response
from hinoki.runtime.rustserver import RustWorker, _drain_native_uninterruptibly
from hinoki.runtime.servercore import build_server_core
from hinoki.settings import AppSettings, CorsSettings
from hinoki.utils import utc_now


class PlanItem(Model, table="crud_plan_items"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    label: str = "model-default"


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


class PlanItems(CrudAPI, model=PlanItem, path="/plan-items"):
    read_schema = PlanItemOut
    create_schema = PlanItemIn
    update_schema = PlanItemPatch
    deletable = True
    public_writes = True


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
    assert plan.model_defaults() == [("id", "uuid4", None)]
    assert plan.create_defaults == [("label", "str", "schema-default")]
    assert plan.update_columns == ["name", "label"]


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
        monkeypatch.setattr(PlanItems, "perform_create", lambda _self, *_args: None)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = plan_pipeline()

    def fail_selection(*_args: object) -> tuple[None, str]:
        raise RuntimeError("test selection failure")

    monkeypatch.setattr(servercore_module, "select_crud_plan", fail_selection)
    build_server_core(pipeline)
    assert all(selection is None for selection in pipeline.crud_plan_selections)
    assert set(pipeline.crud_plan_reasons) == {"native_plan_build_failed"}


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

        worker = object.__new__(RustWorker)
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
    worker = RustWorker(app, "127.0.0.1", 0)
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
    worker = RustWorker(app, "127.0.0.1", 0)
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
    worker = RustWorker(app, "127.0.0.1", 0)
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
    worker = RustWorker(app, "127.0.0.1", 0)
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
    fields = dict(PlanItem.__field_types__)
    fields["name"] = dataclasses.replace(fields["name"], kind="unsupported")
    monkeypatch.setattr(PlanItem, "__field_types__", fields)
    assert crudplan_module.model_fields(PlanItem) is None
    monkeypatch.setattr(PlanItem, "__field_types__", {})
    assert crudplan_module.model_fields(PlanItem) is None

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
