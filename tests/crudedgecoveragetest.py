"""Generated CRUD helpers at schema, query and hook race boundaries."""

from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

import hinoki.resources as crud_module
from hinoki import API, Caller, Schema, crud
from hinoki.context import sync_runner
from hinoki.errors import Forbidden, RegistrationError
from hinoki.http.request import Request
from hinoki.orm import Model, field
from hinoki.pagination import PageParams
from hinoki.resources import CrudBase
from tests.crudtest import Memo, MemoIn, MemoOut, MemoPatch, Memos


class _WireSelection:
    plan = object()

    def __init__(self, valid: bool) -> None:
        self.valid = valid

    def valid_window(self, *_args: Any, **_kwargs: Any) -> bool:
        return self.valid


class _WireQuery:
    order: tuple[tuple[str, bool], ...] = (("id", False),)

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def limit(self, _size: int) -> _WireQuery:
        return self

    def offset(self, _offset: int) -> _WireQuery:
        return self

    async def apaginate(self, _page: PageParams) -> Any:
        return SimpleNamespace(items=self.rows, page=2, page_size=5, total=9)

    async def acount(self) -> int:
        return 9

    def compile(self, *, dialect: str) -> tuple[str, list[Any]]:
        assert dialect == "postgres"
        return "SELECT id", []

    async def _hydrate(self, _found: object) -> list[Any]:
        return self.rows


class _WireDatabase:
    dialect = "postgres"

    def __init__(self, result: bytes | list[dict[str, Any]]) -> None:
        self.result = result

    async def _fetch_page_wire(self, *_args: Any) -> bytes | list[dict[str, Any]]:
        return self.result


async def _wire_schema_items(_self: CrudBase, rows: list[Any]) -> list[str]:
    return [str(row.id) for row in rows]


def test_crud_config_rejects_abstract_and_derived_schemas() -> None:
    class AbstractEntry(Model, abstract=True):
        title: str

    class AbstractEntryOut(Schema):
        title: str

    with pytest.raises(RegistrationError, match="is abstract"):
        crud_module._CrudConfig(model=AbstractEntry, read=AbstractEntryOut).check()

    class OtherEntry(Model, table="crud_edge_other_entries"):
        id: int = field(primary_key=True)

    class OtherEntryOut(Schema, model=OtherEntry):
        id: int

    with pytest.raises(RegistrationError, match="read_schema is derived"):
        crud_module._CrudConfig(model=Memo, read=OtherEntryOut).check()

    with pytest.raises(RegistrationError, match="create_schema is derived"):
        crud_module._CrudConfig(model=Memo, read=MemoOut).check_derived_model(
            "create", OtherEntryOut
        )


@pytest.mark.asyncio
async def test_crud_config_scope_fetch_written_and_owner_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Caller(id="ann", user="ann")
    key = uuid4()
    scoped = Memos.resource.scope(key, principal)
    assert scoped.filters == (
        ("id", "eq", [key]),
        ("owner", "eq", ["ann"]),
    )

    row = SimpleNamespace(id=key, owner="ann")
    calls = 0

    async def get(**conditions: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Memo.NotFound()
        assert conditions == {"id": key}
        return row

    monkeypatch.setattr(Memo, "aget", get)
    assert await Memos.resource.written(key) is row
    assert await Memos.resource.fetch(key) is row

    async def other_owner(**_conditions: Any) -> Any:
        return SimpleNamespace(id=key, owner="bob")

    monkeypatch.setattr(Memo, "aget", other_owner)
    with pytest.raises(Forbidden):
        await Memos.resource.guard(key, principal)


@pytest.mark.asyncio
async def test_wire_list_rows_falls_back_and_handles_wire_bytes_or_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = Memos()
    page = PageParams(page=2, page_size=5)
    rows = [SimpleNamespace(id=uuid4())]
    query = _WireQuery(rows)
    monkeypatch.setattr(crud_module, "schema_items", _wire_schema_items)
    fallback = await crud_module.wire_list_rows(
        api, cast(Any, query), page, cast(Any, _WireSelection(False))
    )
    assert fallback.page == 2 and fallback.total == 9 and len(fallback.items) == 1
    monkeypatch.setattr(crud_module, "current_database", lambda: _WireDatabase(b"[]"))
    reply = await crud_module.wire_list_rows(
        api, cast(Any, query), page, cast(Any, _WireSelection(True))
    )
    assert reply.body == b"[]"
    assert (b"content-type", b"application/json") in reply.headers

    monkeypatch.setattr(
        crud_module, "current_database", lambda: _WireDatabase([{"id": 1}])
    )
    hydrated = await crud_module.wire_list_rows(
        api, cast(Any, query), page, cast(Any, _WireSelection(True))
    )
    assert hydrated.page == 2 and hydrated.total == 9 and len(hydrated.items) == 1


@pytest.mark.asyncio
async def test_list_rows_selects_python_fallback_or_row_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Query:
        order: tuple[tuple[str, bool], ...] = ()

        def order_by(self, name: str) -> Query:
            assert name == "id"
            self.order = ((name, False),)
            return self

    query = Query()

    class APIStub:
        resource = Memos.resource

        async def call_hook(self, _name: str, _principal: Any) -> Query:
            return query

    class Selection:
        def __init__(self, valid: bool, query_valid: bool) -> None:
            self.valid = valid
            self.query_valid = query_valid

        def armed_for(self, _api: Any) -> bool:
            return self.valid

        def valid_query(self, _query: Any) -> bool:
            return self.query_valid

    async def python_rows(*_args: Any) -> str:
        return "python"

    async def wire_rows(*_args: Any) -> str:
        return "wire"

    monkeypatch.setattr(crud_module, "python_list_rows", python_rows)
    monkeypatch.setattr(crud_module, "wire_list_rows", wire_rows)
    api = cast(CrudBase, APIStub())
    request = cast(Request, object())
    page = PageParams(page=1, page_size=20)
    assert (
        await crud_module.list_rows(
            api, request, page, _row_plan=cast(Any, Selection(False, True))
        )
        == "python"
    )
    assert (
        await crud_module.list_rows(
            api, request, page, _row_plan=cast(Any, Selection(True, False))
        )
        == "python"
    )
    assert (
        await crud_module.list_rows(
            api, request, page, _row_plan=cast(Any, Selection(True, True))
        )
        == "wire"
    )
    assert query.order == (("id", False),)


@pytest.mark.asyncio
async def test_schema_items_gathers_async_hooks() -> None:
    class AsyncSchemaHook:
        async def to_schema(self, row: Any) -> str:
            await asyncio.sleep(0)
            return str(row)

        async def call_hook(self, _name: str, row: Any) -> str:
            return await self.to_schema(row)

    hook = cast(CrudBase, AsyncSchemaHook())
    assert await crud_module.schema_items(hook, [1, 2]) == ["1", "2"]


@pytest.mark.asyncio
async def test_generated_create_and_update_reject_hooks_that_change_owned_values() -> (
    None
):
    principal = Caller(id="ann", user="ann")
    key = uuid4()

    class BadCreate:
        resource = Memos.resource

        async def call_hook(
            self, name: str, values: dict[str, Any], _principal: Any
        ) -> Any:
            assert name == "store"
            return Memo(id=uuid4(), owner="bob", title=values["title"], body="")

        def check_written(self, item: Any) -> None:
            if not isinstance(item, Memo):
                raise RegistrationError("wrong model")

    with pytest.raises(RegistrationError, match="changed a server-assigned field"):
        await crud_module.create_row(
            cast(CrudBase, BadCreate()),
            cast(Request, object()),
            MemoIn(title="x"),
            principal,
        )

    visible = Memo(id=key, owner="ann", title="visible", body="")

    class Query:
        def filter(self, **_conditions: Any) -> Query:
            return self

        async def aget(self) -> Memo:
            return visible

    class BadUpdate:
        resource = Memos.resource

        async def call_hook(self, name: str, *_args: Any) -> Any:
            if name == "get_queryset":
                return Query()
            return Memo(id=key, owner="bob", title="changed", body="")

        def check_written(self, item: Any) -> None:
            if not isinstance(item, Memo):
                raise RegistrationError("wrong model")

    with pytest.raises(RegistrationError, match="changed a server-assigned field"):
        await crud_module.update_row(
            cast(CrudBase, BadUpdate()),
            cast(Request, object()),
            key,
            MemoPatch(title="changed"),
            principal,
        )


@pytest.mark.asyncio
async def test_generated_update_rejects_hook_returning_different_primary_key() -> None:
    key = uuid4()
    visible = Memo(id=key, owner="ann", title="visible", body="")

    class Query:
        def filter(self, **_conditions: Any) -> Query:
            return self

        async def aget(self) -> Memo:
            return visible

    class BadUpdate:
        resource = Memos.resource

        async def call_hook(self, name: str, *_args: Any) -> Any:
            return (
                Query()
                if name == "get_queryset"
                else Memo(id=uuid4(), owner="ann", title="changed", body="")
            )

        def check_written(self, item: Any) -> None:
            if not isinstance(item, Memo):
                raise RegistrationError("wrong model")

    with pytest.raises(RegistrationError, match="different item"):
        await crud_module.update_row(
            cast(CrudBase, BadUpdate()),
            cast(Request, object()),
            key,
            MemoPatch(title="changed"),
            Caller(id="ann", user="ann"),
        )


@pytest.mark.asyncio
async def test_generated_delete_race_rechecks_row_and_owner() -> None:
    visible = SimpleNamespace(id=uuid4(), owner="ann")

    class Query:
        def filter(self, **_conditions: Any) -> Query:
            return self

        async def aget(self) -> Any:
            return visible

    class DeleteRace:
        resource = Memos.resource

        async def call_hook(self, name: str, *_args: Any) -> Any:
            return False if name == "remove" else Query()

        async def refuse(self, _item_id: Any, _principal: Any) -> Forbidden:
            return Forbidden("The item could not be changed.")

    with pytest.raises(Forbidden, match="could not be changed"):
        await crud_module.delete_row(
            cast(CrudBase, DeleteRace()),
            cast(Request, object()),
            visible.id,
            Caller(id="ann", user="ann"),
        )


@pytest.mark.asyncio
async def test_crud_hook_type_and_readback_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = Memos()
    with pytest.raises(RegistrationError, match="must return Memo"):
        api.check_written(object())

    class MissingQuery:
        def filter(self, **_conditions: Any) -> MissingQuery:
            return self

        async def aget(self) -> Any:
            raise Memo.NotFound()

    async def missing_hook(_name: str, *_args: Any) -> MissingQuery:
        return MissingQuery()

    monkeypatch.setattr(api, "call_hook", missing_hook)
    with pytest.raises(Memo.NotFound):
        await api.reload_written(uuid4(), Caller(id="ann", user="ann"))


@pytest.mark.asyncio
async def test_refused_update_and_owner_race_messages() -> None:
    item = SimpleNamespace(
        id=uuid4(),
        owner="bob",
        __primary_key__="id",
        has_save_effects=lambda: False,
    )

    class Query:
        def filter(self, **_conditions: Any) -> Query:
            return self

        async def aget(self) -> Any:
            return item

    class APIWithQuery:
        resource = Memos.resource

        async def call_hook(self, _name: str, *_args: Any) -> Query:
            return Query()

    api = cast(CrudBase, APIWithQuery())
    owner_refused = await crud_module.CrudBase.refuse(
        api, item.id, Caller(id="ann", user="ann")
    )
    assert str(owner_refused) == "You cannot change this item."

    item.owner = "ann"
    normal_refused = await crud_module.CrudBase.refuse(
        api, item.id, Caller(id="ann", user="ann")
    )
    assert str(normal_refused) == "The item could not be changed."

    class FailedUpdate:
        async def aupdate(self, _values: dict[str, Any]) -> int:
            return 0

    class UpdateAPI:
        async def refuse(self, _item_id: Any, _principal: Any) -> Forbidden:
            return Forbidden("The item could not be changed.")

    with pytest.raises(Forbidden, match="could not be changed"):
        await crud_module.CrudBase.apply_change(
            cast(CrudBase, UpdateAPI()),
            item,
            cast(Any, FailedUpdate()),
            {"title": "x"},
            None,
        )


@pytest.mark.asyncio
async def test_sync_custom_hook_requires_request_runner_and_awaits_handoff() -> None:
    @crud(Memo, path="/sync-hook-edge", read=MemoOut)
    class SyncHook(API):
        def to_schema(self, item: Any) -> Any:
            return asyncio.sleep(0, result=item)

    api = cast(CrudBase, SyncHook())
    with pytest.raises(RuntimeError, match="running Hinoki request"):
        await api.call_custom_hook("to_schema", "value")

    async def runner(hook: Any, *args: Any) -> Any:
        return hook(*args)

    token = sync_runner.set(runner)
    try:
        assert await api.call_custom_hook("to_schema", "value") == "value"
    finally:
        sync_runner.reset(token)


@pytest.mark.asyncio
async def test_a_reload_that_shows_a_changed_owner_is_refused() -> None:
    principal = Caller(id="ann", user="ann")
    key = uuid4()
    visible = Memo(id=key, owner="ann", title="visible", body="")
    reread = Memo(id=key, owner="bob", title="visible", body="")

    class Query:
        def filter(self, **_conditions: Any) -> Query:
            return self

        async def aget(self) -> Memo:
            return visible

    class Reloading:
        resource = Memos.resource

        async def call_hook(self, name: str, *_args: Any) -> Any:
            return Query() if name == "get_queryset" else visible

        def check_written(self, item: Any) -> None:
            pass

        async def reload_written(self, _key: Any, _principal: Any) -> Memo:
            return reread

    stub = cast(CrudBase, Reloading())
    with pytest.raises(RegistrationError, match="store changed a server-assigned"):
        await crud_module.create_row(
            stub, cast(Request, object()), MemoIn(title="x"), principal
        )
    with pytest.raises(RegistrationError, match="change changed a server-assigned"):
        await crud_module.update_row(
            stub, cast(Request, object()), key, MemoPatch(title="y"), principal
        )


class _CursorDatabase:
    dialect = "postgres"

    def __init__(self, result: Any) -> None:
        self.result = result

    async def _fetch_cursor_wire(self, *_args: Any) -> Any:
        return self.result


@pytest.mark.asyncio
async def test_a_cursor_page_read_on_the_wire_returns_bytes_or_hydrated_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [SimpleNamespace(id=uuid4())]
    cursor = SimpleNamespace(
        codec=None,
        keys=(),
        limit=5,
        mode="forward",
        walk=SimpleNamespace(_hydrate=_WireQuery(rows)._hydrate),
        compile=lambda _dialect: ("SELECT id", []),
        links=lambda _models, _more: ("after-token", None),
    )
    selection = SimpleNamespace(plan=object())
    monkeypatch.setattr(crud_module, "schema_items", _wire_schema_items)
    api = cast(CrudBase, object())
    for result in (b"[]", ([{"id": 1}], True)):
        monkeypatch.setattr(
            crud_module, "current_database", partial(_CursorDatabase, result)
        )
        reply = await crud_module.wire_cursor_rows(
            api, cast(Any, cursor), cast(Any, selection)
        )
        if isinstance(result, bytes):
            assert reply.body == b"[]"
        else:
            assert reply.next == "after-token" and reply.page_size == 5
            assert reply.items == [str(rows[0].id)]


def test_a_crud_base_without_a_resource_declares_nothing() -> None:
    class Bare(CrudBase):
        pass

    assert not hasattr(Bare, "resource")
    assert Bare.routes() == []


def test_a_subclass_keeps_its_own_handwritten_action() -> None:
    @crud(Memo, path="/kept-edge", read=MemoOut)
    class Parent(API):
        pass

    class Child(Parent):
        async def retrieve(self, request: Request, item_id: str) -> str:
            return "mine"

    assert Child.__dict__["retrieve"].__qualname__.endswith("Child.retrieve")


@pytest.mark.asyncio
async def test_an_async_custom_hook_is_awaited_directly() -> None:
    @crud(Memo, path="/async-hook-edge", read=MemoOut)
    class AsyncHook(API):
        async def to_schema(self, item: Any) -> Any:
            return f"async {item}"

    api = cast(CrudBase, AsyncHook())
    assert await api.call_custom_hook("to_schema", "x") == "async x"


def test_crud_omit_refuses_an_unknown_action() -> None:
    with pytest.raises(RegistrationError, match="takes action names from"):
        crud(Memo, path="/omit-edge", read=MemoOut, omit=("archive",))  # type: ignore[arg-type]
