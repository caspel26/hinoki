"""Startup compilation for the private native CRUD plans."""

import dataclasses
import math
import types
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, cast
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    Access,
    BearerAuth,
    Caller,
    CrudAPI,
    Field,
    Hinoki,
    Mount,
    Schema,
    SchemaExtra,
    Unset,
    http,
)
from hinoki import crud as crud_module
from hinoki.db import DatabaseModule
from hinoki.fields import UNSET
from hinoki.orm import Model, QuerySet, field
from hinoki.orm.queryset import write_statement
from hinoki.runtime import crudplan as crudplan_module
from hinoki.runtime import servercore as servercore_module
from hinoki.runtime.binding import Binding
from hinoki.runtime.crudplan import select_crud_plan
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import build_server_core
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


def variant_pipeline(read: type[Schema], **attributes: object) -> Pipeline:
    def fill(namespace: dict[str, object]) -> None:
        namespace.update(read_schema=read, public_writes=True, **attributes)

    owner = types.new_class(
        "Variant", (CrudAPI,), {"model": NoteItem, "path": "/notes"}, fill
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
        (Model, "refresh"),
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
    "attributes",
    [
        {"access": Access(auth=Tokens(), permissions=[])},
        {"read_access": Access(auth=Tokens(), permissions=[])},
        {"access": Access(auth=Tokens(), permissions=[]), "owner": "name"},
        {"access": Access(auth=Tokens(), permissions=[]), "assign": {"name": str}},
        {"get_queryset": custom_queryset},
        {"to_schema": lambda _self, item: item},
    ],
    ids=["access", "read_access", "owner", "assign", "get_queryset", "to_schema"],
)
def test_customized_owner_keeps_lane_c(attributes: dict[str, object]) -> None:
    assert reasons(variant_pipeline(PlainRead, **attributes)) <= {
        "custom_route_or_access_policy",
        "custom_owner_type",
    }
    assert "eligible" not in reasons(variant_pipeline(PlainRead, **attributes))


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
