"""Model field constraints run immediately before ORM writes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from hinoki import Field
from hinoki.db import Database
from hinoki.errors import FieldError, ModelValidationError, RegistrationError
from hinoki.orm import Model, field, use_database
from hinoki.orm.model import (
    _constraint_passes,
    _matches_model_annotation,
    _matches_model_scalar,
    _stop_custom_awaitable,
    own_annotations,
    same_type,
)
from hinoki.runtime.responses import builtin_error

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


def no_whitespace(value: str) -> None:
    if value != value.strip():
        raise ValueError("surrounding whitespace is not allowed")


def accept_json(value: Any) -> None:
    return None


class ValidatedRecord(Model, table="model_validator_records"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    code: str = field(
        max_length=12,
        constraints=Field(min_length=3, pattern=r"[A-Z0-9-]+"),
    )
    label: str = field(max_length=40, validators=(no_whitespace,))
    amount: int = field(constraints=Field(ge=1, le=50))
    state: str = field(constraints=Field(one_of=("draft", "ready")), default="draft")


class TypedJsonRecord(Model, table="typed_json_records"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    data: dict[str, int] = field(validators=(accept_json,))
    numbers: list[int] = field(validators=(accept_json,))


class UnconstrainedRecord(Model, table="unconstrained_records"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str


def test_model_field_constraints_aggregate_stable_details() -> None:
    with pytest.raises(ModelValidationError) as raised:
        ValidatedRecord.validate_values(
            {
                "code": "x!",
                "label": " padded ",
                "amount": 0,
                "state": "missing",
            }
        )
    assert [(item.path, item.code) for item in raised.value.details] == [
        ("code", "min_length"),
        ("code", "pattern"),
        ("label", "invalid"),
        ("amount", "ge"),
        ("state", "one_of"),
    ]
    assert raised.value.details[2].message == "surrounding whitespace is not allowed"


def test_model_validation_is_strict_about_types_and_maps_to_422() -> None:
    with pytest.raises(ModelValidationError) as raised:
        ValidatedRecord.validate_values(
            {"code": "ABC", "label": "Valid", "amount": "2", "state": "draft"}
        )
    assert [(item.path, item.code) for item in raised.value.details] == [
        ("amount", "type")
    ]
    response = builtin_error(raised.value)
    assert response is not None and response.status == 422
    ValidatedRecord.validate_values({"label": "Valid", "amount": 1})


def test_unconstrained_model_fields_keep_existing_type_behavior() -> None:
    UnconstrainedRecord.validate_values({"title": 123})


def test_model_validation_checks_nested_json_types_but_not_rows_read() -> None:
    with pytest.raises(ModelValidationError) as raised:
        TypedJsonRecord.validate_values(
            {"data": "not a mapping", "numbers": [1, "wrong"]}
        )
    assert [(item.path, item.code) for item in raised.value.details] == [
        ("data", "type"),
        ("numbers", "type"),
    ]
    TypedJsonRecord.validate_values({"data": {"count": 1}, "numbers": [1, 2]})
    historical = TypedJsonRecord.from_row(
        {"id": uuid4(), "data": {"count": "legacy"}, "numbers": ["legacy"]}
    )
    legacy_data: Any = historical.data
    assert legacy_data == {"count": "legacy"}


def test_async_callable_and_returned_awaitable_are_not_ignored() -> None:
    class AsyncCallable:
        async def __call__(self, value: str) -> None:
            return None

    invalid_callable: Any = AsyncCallable()
    with pytest.raises(RegistrationError, match="must be synchronous"):

        class AsyncCallableModel(Model, table="async_callable_model"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            value: str = field(validators=(invalid_callable,))

    async def later() -> None:
        return None

    def returns_awaitable(value: str) -> Any:
        return later()

    callback: Any = returns_awaitable

    class AwaitableModel(Model, table="returned_awaitable_model"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        value: str = field(validators=(callback,))

    with pytest.raises(TypeError, match="must be synchronous"):
        AwaitableModel.validate_values({"value": "x"})


async def test_returned_task_is_cancelled_before_validation_fails() -> None:
    completed = asyncio.Event()

    async def worker() -> None:
        completed.set()

    tasks: list[asyncio.Task[None]] = []

    def schedules_task(value: str) -> Any:
        task = asyncio.create_task(worker())
        tasks.append(task)
        return task

    callback: Any = schedules_task

    class ScheduledModel(Model, table="scheduled_model_validator"):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        value: str = field(validators=(callback,))

    with pytest.raises(TypeError, match="must be synchronous"):
        ScheduledModel.validate_values({"value": "x"})
    await asyncio.gather(*tasks, return_exceptions=True)
    assert tasks[0].cancelled()
    assert not completed.is_set()


def test_invalid_constraints_and_async_validators_fail_at_declaration() -> None:
    with pytest.raises(RegistrationError, match="do not apply"):

        class InvalidConstraint(Model, table="invalid_model_constraint"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            count: int = field(constraints=Field(min_length=1))

    async def async_check(value: str) -> None:
        return None

    invalid_validator: Any = async_check
    with pytest.raises(RegistrationError, match="must be synchronous"):

        class AsyncValidator(Model, table="async_model_validator"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            name: str = field(validators=(invalid_validator,))


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2, wait=0.5)
    await database.execute("DROP TABLE IF EXISTS model_validator_records")
    await database.execute(
        "CREATE TABLE model_validator_records (id uuid PRIMARY KEY, "
        "code text NOT NULL, "
        "label text NOT NULL, amount int NOT NULL, state text NOT NULL)"
    )
    with use_database(database):
        yield database
    await database.execute("DROP TABLE IF EXISTS model_validator_records")
    database.close()


@needs_database
async def test_invalid_create_and_bulk_update_do_not_write(db: Database) -> None:
    with pytest.raises(ModelValidationError):
        await ValidatedRecord.acreate(code="bad!", label="ok", amount=2)
    assert await db.fetch_one(
        "SELECT count(*) AS count FROM model_validator_records"
    ) == {"count": 0}

    record = await ValidatedRecord.acreate(code="ABC-1", label="Valid", amount=2)
    with pytest.raises(ModelValidationError):
        await ValidatedRecord.filter(id=record.id).aupdate({"amount": 0})
    assert (await ValidatedRecord.aget(id=record.id)).amount == 2


@needs_database
async def test_instance_save_rejects_then_accepts_corrected_value(db: Database) -> None:
    record = await ValidatedRecord.acreate(code="ABC-2", label="Valid", amount=2)
    record.label = " invalid "
    with pytest.raises(ModelValidationError) as raised:
        await record.asave()
    assert raised.value.details[0].path == "label"
    assert (await ValidatedRecord.aget(id=record.id)).label == "Valid"

    record.label = "Corrected"
    await record.asave()
    assert (await ValidatedRecord.aget(id=record.id)).label == "Corrected"


def test_model_annotation_helpers_handle_forward_and_mixed_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lazy:
        value: int

    fake = SimpleNamespace(
        Format=SimpleNamespace(FORWARDREF="forward"),
        get_annotations=lambda cls, *, format: {"seen": (cls.__name__, format)},
    )
    monkeypatch.setattr("hinoki.orm.model.annotationlib", fake, raising=False)
    monkeypatch.setattr("hinoki.orm.model.sys.version_info", (3, 14))
    assert own_annotations(Lazy) == {"seen": ("Lazy", "forward")}
    assert same_type("Unresolved", int)
    assert _matches_model_annotation("anything", "Unresolved")
    assert _matches_model_scalar(2, Any) and _matches_model_scalar(2, object)
    assert _matches_model_scalar(2, float) and not _matches_model_scalar(True, float)
    assert _constraint_passes("lt", 1, 2)


def test_custom_awaitable_cleanup_calls_its_public_methods() -> None:
    events: list[str] = []

    class Awaitable:
        def __await__(self) -> Any:
            yield

        def cancel(self) -> None:
            events.append("cancel")

        def close(self) -> None:
            events.append("close")

    _stop_custom_awaitable(Awaitable())
    assert events == ["cancel", "close"]


def test_model_field_rejects_invalid_constraints_and_validators() -> None:
    invalid_constraints: Any = object()
    with pytest.raises(RegistrationError, match=r"Field\(\.\.\.\)"):
        field(constraints=invalid_constraints)
    with pytest.raises(RegistrationError, match="supported on Schemas"):
        field(constraints=Field(alias="name"))
    with pytest.raises(RegistrationError, match="conflicts"):
        field(max_length=5, constraints=Field(max_length=6))

    invalid_validator: Any = 42
    with pytest.raises(
        RegistrationError, match="each Model validator must be callable"
    ):

        class InvalidValidator(Model, table="invalid_validator_callable"):
            id: UUID = field(primary_key=True, default_factory=uuid4)
            name: str = field(validators=(invalid_validator,))


async def test_bulk_update_guards_and_sync_update_from_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FieldError, match="needs id"):
        await UnconstrainedRecord.aupdate(title="x")
    with pytest.raises(FieldError, match="conditions and values"):
        await UnconstrainedRecord.aupdate_where({}, {"title": "x"})

    record = UnconstrainedRecord(id=UUID(int=1), title="before")

    def complete(awaitable: Any) -> UnconstrainedRecord:
        awaitable.close()
        return record

    monkeypatch.setattr("hinoki.orm.model.run_sync", complete)
    assert record.update_from() is record
