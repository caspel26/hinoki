"""Direct Rust JSON pages for eligible generated CRUD lists."""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import pytest

from hinoki import API, Caller, Field, Hinoki, JSONRenderer, Schema, crud
from hinoki.db import Database, DatabaseModule
from hinoki.orm import Model, QuerySet, SoftDeleteModel, field
from hinoki.resources import CrudBase
from hinoki.runtime import rowplan
from tests.harness import Client, Feed, Pool, Sink

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")


class MethodObserver:
    """Descriptor that records a patched method and delegates to its original."""

    def __init__(
        self,
        name: str,
        original: Any,
        calls: list[str],
        *,
        mode: str = "",
        records: list[Any] | None = None,
    ) -> None:
        self.name = name
        self.original = original
        self.calls = calls
        self.mode = mode
        self.records = records

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        del owner
        if instance is None:
            return self
        method = (
            self.invoke_async
            if inspect.iscoroutinefunction(self.original)
            else self.invoke_sync
        )
        return functools.partial(method, instance)

    def invoke_sync(self, instance: object, *args: Any, **kwargs: Any) -> Any:
        self.record_call(args)
        if self.mode == "add-filter":
            instance.filters += (("id", "eq", [999]),)  # type: ignore[attr-defined]
        result = self.original(instance, *args, **kwargs)
        if self.mode == "add-one-hundred":
            result.number += 100
        return result

    async def invoke_async(self, instance: object, *args: Any, **kwargs: Any) -> Any:
        self.record_call(args)
        result = await self.original(instance, *args, **kwargs)
        if self.mode == "results" and self.records is not None:
            self.records.append(result)
        return result

    def record_call(self, args: tuple[Any, ...]) -> None:
        if (
            self.mode == "selects"
            and args[0].lstrip().lower().startswith("select")
            and self.records is not None
        ):
            self.records.append(args[0])
        if self.mode == "wire" and self.records is not None:
            self.records.append(args[0])
        self.calls.append(self.name)


class CheckObserver:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        del owner
        if instance is None:
            return self
        return self.__call__

    def __call__(self) -> None:
        self.calls.append("schema-check")


class TransparentDescriptor:
    def __init__(self, name: str, calls: list[str] | None = None) -> None:
        self.name = name
        self.calls = calls

    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        del owner
        if instance is None:
            return self
        if self.calls is not None:
            self.calls.append(f"get:{self.name}")
        return vars(instance)[self.name]

    def __set__(self, instance: object, value: object) -> None:
        if self.calls is not None:
            self.calls.append(f"set:{self.name}")
        vars(instance)[self.name] = value


class StaticDescriptorBase:
    inherited = object()
    shadowed = object()


class StaticDescriptor(StaticDescriptorBase):
    __slots__ = ("slot",)
    shadowed = object()
    data_descriptor = TransparentDescriptor("data_descriptor")

    def non_data_descriptor(self) -> StaticDescriptor:
        return self


class StaticMeta(type):
    def __getattribute__(cls, name: str) -> Any:
        if name == "observed":
            return "dynamic lookup must not run"
        return super().__getattribute__(name)


class StaticMetaClass(metaclass=StaticMeta):
    observed = object()


def changed_crud_schema(self: API, item: Any) -> Any:
    del self
    return item


def changed_schema_validate(
    cls: type[Schema], /, instance: Schema, **kwargs: Any
) -> None:
    del cls, instance, kwargs


def changed_native_schema(cls: type[Schema]) -> Any:
    del cls
    return None


class WireRecord(Model, table="row_wire_records"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str
    count: int
    score: float
    active: bool
    created: datetime
    nullable: str | None = None


class WireRecordOut(Schema):
    id: UUID
    name: Annotated[str, Field(alias="displayName")]
    count: int
    score: float
    active: bool
    created: datetime
    nullable: str | None


@crud(WireRecord, path="/rows", read=WireRecordOut)
class WireRecords(API):
    pass


def wire_api() -> CrudBase:
    return cast(CrudBase, WireRecords())


class ConstrainedRecordOut(Schema):
    id: UUID
    name: Annotated[str, Field(min_length=1)]
    count: int
    score: float
    active: bool
    created: datetime
    nullable: str | None


@crud(WireRecord, path="/constrained", read=ConstrainedRecordOut)
class ConstrainedRecords(API):
    pass


class CustomQueryRecords(WireRecords, path="/custom-query"):
    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[WireRecord]:
        return WireRecord.all()


class InstanceHookRecords(WireRecords, path="/instance-hook"):
    def __init__(self) -> None:
        cast(Any, self).get_queryset = self.instance_queryset

    def instance_queryset(self, _principal: Caller[Any] | None) -> QuerySet[WireRecord]:
        return WireRecord.all()


class NewHookRecords(WireRecords, path="/new-hook"):
    def __new__(cls) -> NewHookRecords:
        instance = object.__new__(cls)
        cast(Any, instance).to_schema = instance.projected_schema
        return instance

    def projected_schema(self, item: WireRecord) -> WireRecordOut:
        return WireRecordOut(
            id=item.id,
            name="projected by instance hook",
            count=item.count,
            score=item.score,
            active=item.active,
            created=item.created,
            nullable=item.nullable,
        )


class GetattributeRecords(WireRecords, path="/getattribute"):
    def __getattribute__(self, name: str) -> Any:
        return super().__getattribute__(name)


class GetattrRecords(WireRecords, path="/getattr"):
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


class InvokeHookRecords(WireRecords, path="/invoke-hook"):
    async def call_hook(self, name: str, *arguments: Any) -> Any:
        return await cast(Any, super()).call_hook(name, *arguments)


class DynamicRecord(Model, table="row_wire_dynamic"):
    id: int = field(primary_key=True)
    number: int


class DynamicRecordOut(Schema):
    id: int
    number: int


class DefaultDynamicRecordOut(Schema):
    id: int
    number: int = 0


@crud(DynamicRecord, path="/dynamic", read=DynamicRecordOut)
class DynamicRecords(API):
    pass


class AllocatorMixin:
    def __new__(cls, *args: object, **kwargs: object) -> AllocatedRecord:
        del args, kwargs
        return cast(AllocatedRecord, cast(Any, super()).__new__(cls))


class AllocatedRecord(AllocatorMixin, Model, table="row_wire_allocated"):
    id: int = field(primary_key=True)
    number: int


class AllocatedRecordOut(Schema):
    id: int
    number: int


@crud(AllocatedRecord, path="/allocated", read=AllocatedRecordOut)
class AllocatedRecords(API):
    pass


class SetterOnly:
    def __set__(self, instance: object, value: object) -> None:
        cast(Any, instance).__dict__["number"] = value


class SetterMixin:
    number = SetterOnly()


class SetterRecord(SetterMixin, Model, table="row_wire_setter"):
    id: int = field(primary_key=True)
    number: int  # type: ignore[assignment]


class SetterRecordOut(Schema):
    id: int
    number: int


@crud(SetterRecord, path="/setter", read=SetterRecordOut)
class SetterRecords(API):
    pass


class DeleteOnly:
    def __delete__(self, instance: object) -> None:
        del instance


class DeleteMixin:
    number = DeleteOnly()


class DeleteRecord(DeleteMixin, Model, table="row_wire_delete"):
    id: int = field(primary_key=True)
    number: int  # type: ignore[assignment]


class DeleteRecordOut(Schema):
    id: int
    number: int


@crud(DeleteRecord, path="/delete-descriptor", read=DeleteRecordOut)
class DeleteRecords(API):
    pass


class ValidatorMixinSchema(Schema):
    @classmethod
    def _validate_instance(cls, instance: Schema, **kwargs: Any) -> None:
        super()._validate_instance(instance, **kwargs)


class ValidatorRecordOut(ValidatorMixinSchema):
    id: int
    number: int


class DynamicValidatorRecord(Model, table="row_wire_validator"):
    id: int = field(primary_key=True)
    number: int


@crud(DynamicValidatorRecord, path="/validator", read=ValidatorRecordOut)
class ValidatorRecords(API):
    pass


class NativeSchemaMixin:
    @classmethod
    def native_schema(cls) -> Any:
        return cast(Any, super()).native_schema()


class NativeSchemaOut(NativeSchemaMixin, Schema):
    id: int
    number: int


@crud(DynamicValidatorRecord, path="/native-schema", read=NativeSchemaOut)
class NativeSchemaRecords(API):
    pass


SPEC_CALLS: list[str] = []


class ProbedSpecsOut(Schema):
    id: int
    number: int

    @classmethod
    def specs(cls) -> dict[str, Any]:
        SPEC_CALLS.append("specs")
        return cast(dict[str, Any], cast(Any, super()).specs())


@crud(DynamicValidatorRecord, path="/probed-specs", read=ProbedSpecsOut)
class ProbedSpecsRecords(API):
    pass


class HiddenTimestampRecord(Model, table="row_wire_hidden_timestamp"):
    id: int = field(primary_key=True)
    hidden: datetime


class HiddenTimestampOut(Schema):
    id: int


@crud(HiddenTimestampRecord, path="/hidden-time", read=HiddenTimestampOut)
class HiddenTimestampRecords(API):
    pass


class AlternateAllRecord(Model, table="row_wire_alternate_all"):
    id: int = field(primary_key=True)
    number: int

    def __post_init__(self) -> None:
        self.number += 100


class CustomAllRecord(Model, table="row_wire_custom_all"):
    id: int = field(primary_key=True)
    number: int

    @classmethod
    def all(cls) -> QuerySet[CustomAllRecord]:
        return cast(QuerySet[CustomAllRecord], AlternateAllRecord.all())


class CustomAllOut(Schema):
    id: int
    number: int


@crud(CustomAllRecord, path="/custom-all", read=CustomAllOut)
class CustomAllRecords(API):
    pass


async def raw_request(
    client: Client, method: str, path: str, query: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    sink = Sink()
    await client.adapter(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [(b"host", b"localhost"), (b"accept", b"application/json")],
        },
        Feed({"type": "http.request", "body": b"", "more_body": False}),
        sink,
    )
    start, end = sink.sent
    return start, end


def public_headers(message: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    return [
        (name, value) for name, value in message["headers"] if name != b"x-request-id"
    ]


def test_row_wire_selection_is_per_app_and_not_in_the_public_contract() -> None:
    standard = Hinoki(include=[WireRecords])
    formatted = Hinoki(renderers=[JSONRenderer(indent=2)], include=[WireRecords])
    assert standard.bindings[0].row_wire is not None
    assert formatted.bindings[0].row_wire is None
    signature = cast(Any, standard.bindings[0].route.handler).__signature__
    assert "_row_plan" not in str(signature)
    assert standard.openapi() == formatted.openapi()


def test_class_static_matches_getattr_static_for_every_row_guard_name() -> None:
    owners = (
        WireRecords,
        WireRecord,
        WireRecordOut,
        QuerySet,
        StaticDescriptorBase,
        StaticDescriptor,
        StaticMetaClass,
    )
    guard_names = {
        *rowplan.OWNER_GUARD_NAMES,
        *rowplan.MODEL_GUARD_NAMES,
        *rowplan.SCHEMA_GUARD_NAMES,
        *rowplan.QUERYSET_GUARD_NAMES,
        *(name for name, _ in WireRecord.__field_types__.items()),
        *(name for name in WireRecordOut.specs()),
        "__name__",
        "__doc__",
        "__dict__",
        "__bases__",
        "__mro__",
        "__module__",
        "__weakref__",
        "inherited",
        "shadowed",
        "slot",
        "data_descriptor",
        "non_data_descriptor",
        "observed",
        "missing_attribute",
    }
    for owner in owners:
        for name in guard_names:
            assert rowplan.class_static(owner, name) is inspect.getattr_static(
                owner, name, None
            ), (owner.__qualname__, name)
    calls: list[str] = []
    descriptor_owner = type(
        "DescriptorOwner",
        (),
        {"descriptor": TransparentDescriptor("descriptor", calls)},
    )
    descriptor = descriptor_owner.__dict__["descriptor"]
    assert rowplan.class_static(descriptor_owner, "descriptor") is descriptor
    assert calls == []


def test_custom_metaclass_static_lookup_does_not_run_dynamic_lookup() -> None:
    assert rowplan.class_static(StaticMetaClass, "observed") is inspect.getattr_static(
        StaticMetaClass, "observed", None
    )
    assert rowplan.class_static(StaticMetaClass, "observed") != (
        "dynamic lookup must not run"
    )


def test_ineligible_generated_lists_stay_on_the_python_path() -> None:
    for api in (
        ConstrainedRecords,
        CustomQueryRecords,
        InstanceHookRecords,
        NewHookRecords,
        GetattributeRecords,
        GetattrRecords,
        InvokeHookRecords,
        AllocatedRecords,
        SetterRecords,
        DeleteRecords,
        ValidatorRecords,
        NativeSchemaRecords,
        ProbedSpecsRecords,
        CustomAllRecords,
    ):
        SPEC_CALLS.clear()
        app = Hinoki(include=[api])
        assert app.bindings[0].row_wire is None
        if api is ProbedSpecsRecords:
            assert SPEC_CALLS == []


def test_mutated_hooks_fail_closed_after_registration() -> None:
    app = Hinoki(include=[WireRecords])
    selection = app.bindings[0].row_wire
    assert selection is not None
    original = cast(Any, WireRecords).to_schema
    try:
        cast(Any, WireRecords).to_schema = changed_crud_schema
        assert not selection.valid_for(wire_api())
    finally:
        cast(Any, WireRecords).to_schema = original


def test_mutated_instance_hook_fails_before_row_wire_query() -> None:
    selection = Hinoki(include=[WireRecords]).bindings[0].row_wire
    assert selection is not None
    api = wire_api()
    assert selection.valid_for(api)
    cast(Any, api).to_schema = changed_crud_schema.__get__(api, WireRecords)
    assert not selection.valid_for(api)


def test_owner_guard_falls_back_when_instance_state_is_unavailable() -> None:
    class NoInstanceState:
        __slots__ = ()

    assert rowplan.has_shadowed_owner_state(NoInstanceState())  # type: ignore[arg-type]


def test_row_wire_selection_rejects_non_type_model_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki(include=[WireRecords])
    candidate = SimpleNamespace(model=object(), read=WireRecordOut)
    monkeypatch.setattr(
        rowplan, "eligible_route", lambda _binding: (WireRecords, candidate)
    )
    assert rowplan.select_row_wire(app.bindings[0]) is None


def test_row_wire_query_guards_accept_only_the_expected_window() -> None:
    selection = Hinoki(include=[WireRecords]).bindings[0].row_wire
    assert selection is not None
    query = WireRecord.all()
    assert selection.valid_query(query)
    window = query.limit(20).offset(0)
    assert selection.valid_window(window, order=query.order, limit=20, offset=0)
    assert not selection.valid_query(object())
    assert not selection.valid_query(query.limit(1))
    malformed: QuerySet[Any] = object.__new__(QuerySet)
    assert not selection.valid_query(malformed)
    with pytest.raises(AttributeError):
        cast(Any, WireRecord.all()).unexpected = True
    assert not selection.valid_window(object(), order=query.order, limit=20, offset=0)
    custom_query_type = type("CustomQuerySet", (QuerySet,), {})
    custom_query: QuerySet[Any] = cast(Any, object.__new__(custom_query_type))
    assert not selection.valid_window(
        custom_query, order=query.order, limit=20, offset=0
    )


def test_row_wire_schema_projection_rejects_defaulted_fields() -> None:
    assert rowplan.schema_fields(DynamicRecord, DefaultDynamicRecordOut) is None


def test_schema_setter_guard_accepts_the_builtin_dataclass_setter() -> None:
    assert rowplan.plain_schema_setattr(Schema)


def test_mutated_schema_and_model_metadata_fail_closed() -> None:
    app = Hinoki(include=[WireRecords])
    selection = app.bindings[0].row_wire
    assert selection is not None

    original_schema = WireRecordOut.specs()["name"]
    original_model = WireRecord.__field_types__["name"]
    try:
        WireRecordOut.specs()["name"] = dataclasses.replace(
            original_schema, alias="changed"
        )
        assert not selection.valid_for(wire_api())
    finally:
        WireRecordOut.specs()["name"] = original_schema

    try:
        WireRecord.__field_types__["name"] = dataclasses.replace(
            original_model, kind="int"
        )
        assert not selection.valid_for(wire_api())
    finally:
        WireRecord.__field_types__["name"] = original_model


def test_mutated_schema_validator_methods_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = Hinoki(include=[WireRecords]).bindings[0].row_wire
    assert selection is not None

    monkeypatch.setattr(
        WireRecordOut, "_validate_instance", classmethod(changed_schema_validate)
    )
    assert not selection.valid_for(wire_api())
    monkeypatch.undo()

    monkeypatch.setattr(
        WireRecordOut, "native_schema", classmethod(changed_native_schema)
    )
    assert not selection.valid_for(wire_api())


@pytest.mark.skipif(URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_wire_page_matches_python_oracle_byte_for_byte(pool: Pool) -> None:
    assert URL is not None
    admin = await Database.connect(URL)
    await admin.execute("DROP TABLE IF EXISTS row_wire_records")
    await admin.execute(
        "CREATE TABLE row_wire_records (id uuid PRIMARY KEY, name text NOT NULL, "
        "count bigint NOT NULL, score float8 NOT NULL, active boolean NOT NULL, "
        "created timestamptz NOT NULL, nullable text NULL)"
    )
    ident = uuid4()
    timestamp = datetime(2026, 11, 16, 10, 11, 12, 123000, tzinfo=UTC)
    await admin.execute(
        "INSERT INTO row_wire_records VALUES ($1, $2, $3, $4, $5, $6, $7)",
        ident,
        "Hinoki\n🌲",
        2**40,
        -0.0,
        True,
        timestamp,
        None,
    )
    optimized = Hinoki(include=[WireRecords], modules=[DatabaseModule(URL, max_size=2)])
    oracle = Hinoki(include=[WireRecords], modules=[DatabaseModule(URL, max_size=2)])
    oracle._bindings[0] = dataclasses.replace(oracle._bindings[0], row_wire=None)
    fast_client = await pool.start(optimized)
    oracle_client = await pool.start(oracle)
    fast_start, fast_end = await raw_request(fast_client, "GET", "/rows", "page=1")
    slow_start, slow_end = await raw_request(oracle_client, "GET", "/rows", "page=1")
    assert fast_start["status"] == slow_start["status"] == 200
    assert public_headers(fast_start) == public_headers(slow_start)
    assert fast_end["body"] == slow_end["body"]
    assert b'"displayName":"Hinoki\\n\xf0\x9f\x8c\xb2"' in fast_end["body"]
    assert b'"created":"2026-11-16T10:11:12.123000+00:00"' in fast_end["body"]
    projected = Hinoki(
        include=[NewHookRecords], modules=[DatabaseModule(URL, max_size=2)]
    )
    projected_client = await pool.start(projected)
    _, projected_end = await raw_request(projected_client, "GET", "/new-hook")
    assert b'"displayName":"projected by instance hook"' in projected_end["body"]
    await admin.execute("DROP TABLE row_wire_records")

    await admin.execute("DROP TABLE IF EXISTS row_wire_hidden_timestamp")
    await admin.execute(
        "CREATE TABLE row_wire_hidden_timestamp ("
        "id integer PRIMARY KEY, hidden timestamptz NOT NULL)"
    )
    await admin.execute(
        "INSERT INTO row_wire_hidden_timestamp VALUES (1, '0001-01-01 BC')"
    )
    hidden_fast = Hinoki(
        include=[HiddenTimestampRecords], modules=[DatabaseModule(URL, max_size=2)]
    )
    hidden_oracle = Hinoki(
        include=[HiddenTimestampRecords], modules=[DatabaseModule(URL, max_size=2)]
    )
    hidden_oracle._bindings[0] = dataclasses.replace(
        hidden_oracle._bindings[0], row_wire=None
    )
    hidden_fast_client = await pool.start(hidden_fast)
    hidden_oracle_client = await pool.start(hidden_oracle)
    hidden_fast_start, hidden_fast_end = await raw_request(
        hidden_fast_client, "GET", "/hidden-time"
    )
    hidden_oracle_start, hidden_oracle_end = await raw_request(
        hidden_oracle_client, "GET", "/hidden-time"
    )
    assert hidden_fast_start["status"] == hidden_oracle_start["status"] == 500
    assert public_headers(hidden_fast_start) == public_headers(hidden_oracle_start)
    hidden_fast_error = json.loads(hidden_fast_end["body"])
    hidden_oracle_error = json.loads(hidden_oracle_end["body"])
    hidden_fast_error.pop("request_id")
    hidden_oracle_error.pop("request_id")
    assert hidden_fast_error == hidden_oracle_error
    await admin.execute("DROP TABLE row_wire_hidden_timestamp")
    admin.close()


@pytest.mark.skipif(URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_mutated_checks_fall_back_before_count_and_wire_fetch(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert URL is not None
    admin = await Database.connect(URL)
    await admin.execute("DROP TABLE IF EXISTS row_wire_records")
    await admin.execute(
        "CREATE TABLE row_wire_records (id uuid PRIMARY KEY, name text NOT NULL, "
        "count bigint NOT NULL, score float8 NOT NULL, active boolean NOT NULL, "
        "created timestamptz NOT NULL, nullable text NULL)"
    )
    await admin.execute(
        "INSERT INTO row_wire_records VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid4(),
        "check mutation",
        1,
        1.5,
        True,
        datetime(2026, 11, 16, 10, 11, 12, tzinfo=UTC),
        None,
    )
    app = Hinoki(include=[WireRecords], modules=[DatabaseModule(URL, max_size=2)])
    selection = app.bindings[0].row_wire
    assert selection is not None
    checks: list[str] = []
    original_checks = WireRecordOut.__checks__
    WireRecordOut.observed_check = CheckObserver(checks)  # type: ignore[attr-defined]
    WireRecordOut.__checks__ = (("object", (), "observed_check"),)
    # The negative graph-check cache assumes checks never change after class
    # creation; this test mutates them on purpose, so drop the cached answer.
    WireRecordOut.__graph_checks__ = None
    counts: list[str] = []
    selects: list[str] = []
    wire_calls: list[str] = []
    monkeypatch.setattr(
        QuerySet,
        "acount",
        MethodObserver("count", QuerySet.acount, counts),
    )
    monkeypatch.setattr(
        Database,
        "fetch",
        MethodObserver("fetch", Database.fetch, [], mode="selects", records=selects),
    )
    monkeypatch.setattr(
        Database,
        "fetch_models",
        MethodObserver(
            "fetch_models", Database.fetch_models, [], mode="selects", records=selects
        ),
    )
    monkeypatch.setattr(
        Database,
        "_fetch_page_wire",
        MethodObserver(
            "_fetch_page_wire",
            Database._fetch_page_wire,
            [],
            mode="wire",
            records=wire_calls,
        ),
    )
    try:
        client = await pool.start(app)
        start, end = await raw_request(client, "GET", "/rows")
        assert start["status"] == 200
        assert b'"displayName":"check mutation"' in end["body"]
        assert checks == ["schema-check"]
        assert counts == ["count"]
        assert len(selects) == 2
        assert wire_calls == []
    finally:
        WireRecordOut.__checks__ = original_checks
        del WireRecordOut.observed_check  # type: ignore[attr-defined]
        await admin.execute("DROP TABLE row_wire_records")
        admin.close()


@pytest.mark.skipif(URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("owner", [WireRecord, WireRecordOut], ids=["model", "schema"])
async def test_field_descriptor_added_after_include_uses_python_path_before_query(
    owner: type[Any], pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert URL is not None
    admin = await Database.connect(URL)
    await admin.execute("DROP TABLE IF EXISTS row_wire_records")
    await admin.execute(
        "CREATE TABLE row_wire_records (id uuid PRIMARY KEY, name text NOT NULL, "
        "count bigint NOT NULL, score float8 NOT NULL, active boolean NOT NULL, "
        "created timestamptz NOT NULL, nullable text NULL)"
    )
    await admin.execute(
        "INSERT INTO row_wire_records VALUES ($1, $2, $3, $4, $5, $6, $7)",
        uuid4(),
        "descriptor mutation",
        1,
        1.5,
        True,
        datetime(2026, 11, 16, 10, 11, 12, tzinfo=UTC),
        None,
    )
    app = Hinoki(include=[WireRecords], modules=[DatabaseModule(URL, max_size=2)])
    selection = app.bindings[0].row_wire
    assert selection is not None
    descriptor_calls: list[str] = []
    monkeypatch.setattr(
        owner,
        "name",
        TransparentDescriptor("name", descriptor_calls),
        raising=False,
    )
    assert not selection.valid_for(wire_api())

    counts: list[str] = []
    selects: list[str] = []
    wire_calls: list[str] = []
    monkeypatch.setattr(
        QuerySet, "acount", MethodObserver("count", QuerySet.acount, counts)
    )
    monkeypatch.setattr(
        Database,
        "fetch",
        MethodObserver("fetch", Database.fetch, [], mode="selects", records=selects),
    )
    monkeypatch.setattr(
        Database,
        "fetch_models",
        MethodObserver(
            "fetch_models", Database.fetch_models, [], mode="selects", records=selects
        ),
    )
    monkeypatch.setattr(
        Database,
        "_fetch_page_wire",
        MethodObserver(
            "_fetch_page_wire",
            Database._fetch_page_wire,
            [],
            mode="wire",
            records=wire_calls,
        ),
    )
    try:
        client = await pool.start(app)
        start, end = await raw_request(client, "GET", "/rows", "page=1")
        assert start["status"] == 200
        assert b'"displayName":"descriptor mutation"' in end["body"]
        assert descriptor_calls
        assert counts == ["count"]
        assert len(selects) == 2
        assert wire_calls == []
    finally:
        await admin.execute("DROP TABLE row_wire_records")
        admin.close()


async def test_invalid_sqlite_cell_falls_back_without_a_second_select(
    pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "row-wire.sqlite"
    url = f"sqlite:///{path}"
    admin = await Database.connect(url)
    await admin.execute(
        "CREATE TABLE row_wire_dynamic ("
        "id INTEGER PRIMARY KEY, number INTEGER NOT NULL)"
    )
    await admin.execute("INSERT INTO row_wire_dynamic VALUES (?, ?)", 1, "wrong")
    app = Hinoki(include=[DynamicRecords], modules=[DatabaseModule(url, max_size=2)])
    oracle = Hinoki(include=[DynamicRecords], modules=[DatabaseModule(url, max_size=2)])
    oracle._bindings[0] = dataclasses.replace(oracle._bindings[0], row_wire=None)
    fast_client = await pool.start(app)
    oracle_client = await pool.start(oracle)
    wire_results: list[object] = []
    selects: list[str] = []
    monkeypatch.setattr(
        Database,
        "_fetch_page_wire",
        MethodObserver(
            "_fetch_page_wire",
            Database._fetch_page_wire,
            [],
            mode="results",
            records=wire_results,
        ),
    )
    monkeypatch.setattr(
        Database,
        "fetch",
        MethodObserver("fetch", Database.fetch, [], mode="selects", records=selects),
    )
    monkeypatch.setattr(
        Database,
        "fetch_models",
        MethodObserver(
            "fetch_models", Database.fetch_models, [], mode="selects", records=selects
        ),
    )
    fast_start, fast_end = await raw_request(fast_client, "GET", "/dynamic")
    slow_start, slow_end = await raw_request(oracle_client, "GET", "/dynamic")
    assert wire_results and isinstance(wire_results[0], list)
    assert len(selects) == 3  # one count query per request; no fallback re-query
    assert fast_start["status"] == slow_start["status"] == 500
    assert public_headers(fast_start) == public_headers(slow_start)
    fast_error = json.loads(fast_end["body"])
    slow_error = json.loads(slow_end["body"])
    fast_error.pop("request_id")
    slow_error.pop("request_id")
    assert fast_error == slow_error
    await admin.execute("DROP TABLE row_wire_dynamic")
    admin.close()


async def test_custom_model_all_keeps_python_hydration_semantics(
    pool: Pool, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'custom-all.sqlite'}"
    admin = await Database.connect(url)
    await admin.execute(
        "CREATE TABLE row_wire_alternate_all ("
        "id INTEGER PRIMARY KEY, number INTEGER NOT NULL)"
    )
    await admin.execute("INSERT INTO row_wire_alternate_all VALUES (1, 2)")
    app = Hinoki(include=[CustomAllRecords], modules=[DatabaseModule(url, max_size=2)])
    assert app.bindings[0].row_wire is None
    client = await pool.start(app)
    start, end = await raw_request(client, "GET", "/custom-all")
    assert start["status"] == 200
    assert json.loads(end["body"])["items"] == [{"id": 1, "number": 102}]
    await admin.execute("DROP TABLE row_wire_alternate_all")
    admin.close()


@pytest.mark.parametrize(
    "method_name",
    [
        "derive",
        "order_by",
        "limit",
        "offset",
        "apaginate",
        "__await__",
        "fetch",
        "_hydrate",
        "build",
        "load",
    ],
)
async def test_queryset_customizations_after_include_use_python_path(
    method_name: str,
    pool: Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / f'queryset-{method_name}.sqlite'}"
    admin = await Database.connect(url)
    await admin.execute(
        "CREATE TABLE row_wire_dynamic ("
        "id INTEGER PRIMARY KEY, number INTEGER NOT NULL)"
    )
    await admin.execute("INSERT INTO row_wire_dynamic VALUES (1, 2)")
    app = Hinoki(include=[DynamicRecords], modules=[DatabaseModule(url, max_size=2)])
    assert app.bindings[0].row_wire is not None
    original = getattr(QuerySet, method_name)
    calls: list[str] = []
    monkeypatch.setattr(
        QuerySet, method_name, MethodObserver(method_name, original, calls)
    )
    wire_calls: list[str] = []
    monkeypatch.setattr(
        Database,
        "_fetch_page_wire",
        MethodObserver(
            "_fetch_page_wire",
            Database._fetch_page_wire,
            [],
            mode="wire",
            records=wire_calls,
        ),
    )
    client = await pool.start(app)
    start, end = await raw_request(client, "GET", "/dynamic")
    assert start["status"] == 200
    assert json.loads(end["body"])["items"] == [{"id": 1, "number": 2}]
    assert calls
    assert wire_calls == []
    await admin.execute("DROP TABLE row_wire_dynamic")
    admin.close()


async def test_queryset_build_patch_after_include_preserves_custom_rows(
    pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'queryset-build.sqlite'}"
    admin = await Database.connect(url)
    await admin.execute(
        "CREATE TABLE row_wire_dynamic ("
        "id INTEGER PRIMARY KEY, number INTEGER NOT NULL)"
    )
    await admin.execute("INSERT INTO row_wire_dynamic VALUES (1, 2)")
    app = Hinoki(include=[DynamicRecords], modules=[DatabaseModule(url, max_size=2)])
    original = QuerySet.build
    monkeypatch.setattr(
        QuerySet,
        "build",
        MethodObserver("build", original, [], mode="add-one-hundred"),
    )
    client = await pool.start(app)
    start, end = await raw_request(client, "GET", "/dynamic")
    assert start["status"] == 200
    assert json.loads(end["body"])["items"] == [{"id": 1, "number": 102}]
    await admin.execute("DROP TABLE row_wire_dynamic")
    admin.close()


async def test_custom_queryset_limit_keeps_count_before_window(
    pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'queryset-limit.sqlite'}"
    admin = await Database.connect(url)
    await admin.execute(
        "CREATE TABLE row_wire_dynamic ("
        "id INTEGER PRIMARY KEY, number INTEGER NOT NULL)"
    )
    await admin.execute("INSERT INTO row_wire_dynamic VALUES (1, 2)")
    app = Hinoki(include=[DynamicRecords], modules=[DatabaseModule(url, max_size=2)])
    original = QuerySet.limit
    monkeypatch.setattr(
        QuerySet,
        "limit",
        MethodObserver("limit", original, [], mode="add-filter"),
    )
    client = await pool.start(app)
    start, end = await raw_request(client, "GET", "/dynamic")
    assert start["status"] == 200
    assert json.loads(end["body"]) == {
        "items": [],
        "page": 1,
        "page_size": 20,
        "total": 1,
        "pages": 1,
    }
    await admin.execute("DROP TABLE row_wire_dynamic")
    admin.close()


def test_queryset_patch_before_include_is_not_snapshotted_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        QuerySet,
        "build",
        MethodObserver("build", QuerySet.build, []),
    )
    app = Hinoki(include=[DynamicRecords])
    assert app.bindings[0].row_wire is None


def test_row_wire_class_guards_are_frozen_per_generation() -> None:
    selection = Hinoki(include=[WireRecords]).bindings[0].row_wire
    assert selection is not None
    original = cast(Any, WireRecords).to_schema
    try:
        assert selection.armed_for(wire_api())
        cast(Any, WireRecords).to_schema = changed_crud_schema
        assert selection.armed_for(wire_api())
        rowplan.refreeze_row_wire()
        assert not selection.armed_for(wire_api())
    finally:
        cast(Any, WireRecords).to_schema = original
    rowplan.refreeze_row_wire()
    assert selection.armed_for(wire_api())
    api = wire_api()
    cast(Any, api).to_schema = changed_crud_schema.__get__(api, WireRecords)
    assert not selection.armed_for(api)


class SoftWireRecord(SoftDeleteModel, table="row_wire_soft_records"):
    id: int = field(primary_key=True)
    name: str


class SoftWireRecordOut(Schema):
    id: int
    name: str


@crud(SoftWireRecord, path="/soft-rows", read=SoftWireRecordOut)
class SoftWireRecords(API):
    pass


def test_soft_delete_lists_accept_only_their_live_row_filter() -> None:
    selection = Hinoki(include=[SoftWireRecords]).bindings[0].row_wire
    assert selection is not None
    assert selection.valid_query(SoftWireRecord.all())
    assert not selection.valid_query(SoftWireRecord.with_deleted())
    assert not selection.valid_query(SoftWireRecord.all().filter(name="x"))
