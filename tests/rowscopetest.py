"""Tests for Caller.claims, RowScope, crud(..., scope=...), and Rust CrudPlan parity."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, cast
from uuid import UUID, uuid4

import pytest

from hinoki import (
    API,
    UNSET,
    Access,
    BearerAuth,
    Caller,
    Field,
    Hinoki,
    RowScope,
    Schema,
    Unset,
    core,
    crud,
    errors,
    from_user,
    http,
)
from hinoki.crypto import ClaimsJWTAuth, JWTKey
from hinoki.db import Database, DatabaseModule
from hinoki.errors import Forbidden, RegistrationError
from hinoki.filters import Eq, FilterSet, Search
from hinoki.orm import (
    ManyToMany,
    Model,
    QuerySet,
    field,
    many_to_many,
    migrator,
    use_database,
    uuid_pk,
)
from hinoki.orm.migrations import create_table_sql, plan, table_state
from hinoki.pagination import PageNumberPagination, PageParams
from hinoki.resources import caller_id, create_row, list_rows, retrieve_row
from hinoki.runtime.crudplan import select_crud_plan
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import build_server_core, native_handler_eligible
from hinoki.testing import ASGIClient

JWT_SECRET = b"row-scope-test-secret-32-bytes!!"


@dataclasses.dataclass(frozen=True, slots=True)
class User:
    id: str
    tenant_id: str


class AbstractItem(Model, abstract=True):
    tenant_id: str


class Tag(Model, table="rs_tags"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    tenant_id: str = field(max_length=32)
    name: str = field(max_length=40)


class Invoice(Model, table="rs_invoices"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    tenant_id: str = field(max_length=32)
    owner_id: str = field(max_length=32)
    title: str = field(max_length=60)
    archived: bool = False
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag, ids_name="tag_ids", max_items=5)


TagIds = Annotated[set[UUID], Field(max_length=5)]


class InvoiceOut(Schema):
    id: UUID
    tenant_id: str
    owner_id: str
    title: str
    archived: bool
    tag_ids: TagIds


class InvoiceIn(Schema):
    title: str
    tag_ids: TagIds | Unset = UNSET


class InvoicePatch(Schema):
    title: str | Unset = UNSET
    archived: bool | Unset = UNSET
    tag_ids: TagIds | Unset = UNSET


class InvoiceFilters(FilterSet):
    archived: Eq[bool]
    search = Search("title")
    ordering = ("title",)


USERS = {
    "alice-token": User(id="alice", tenant_id="acme"),
    "aaron-token": User(id="aaron", tenant_id="acme"),
    "bob-token": User(id="bob", tenant_id="globex"),
}


class TenantAuth(BearerAuth[User]):
    async def resolve_token(self, token: str) -> Caller[User] | None:
        user = USERS.get(token.strip())
        if user is None:
            return None
        return Caller(id=user.id, user=user, permissions=frozenset({"invoices:use"}))


staff = Access(auth=TenantAuth(), permissions=["invoices:use"])

invoice_scope: RowScope[Invoice, User] = RowScope.by(
    Invoice, "tenant_id", from_user("tenant_id")
)
tag_scope: RowScope[Tag, User] = RowScope.by(Tag, "tenant_id", from_user("tenant_id"))


async def reopen_invoice(item_id: UUID, caller: Caller[User]) -> Invoice:
    """Domain operation reusing ``invoice_scope`` with matching owner check."""
    row = await invoice_scope.apply(Invoice.all(), caller).filter(id=item_id).aget()
    if row.owner_id != caller.id:
        raise errors.Forbidden("You cannot change this item.")
    if not row.archived:
        raise errors.Conflict("The invoice is already active.")
    tenant = invoice_scope.value_for(caller)
    updated = await Invoice.aupdate_where(
        {"id": item_id, "tenant_id": tenant, "owner_id": caller.id, "archived": True},
        {"archived": False},
    )
    if updated:
        return updated[0]
    raise errors.Conflict("The invoice is already active.")


@crud(
    Invoice,
    path="/invoices",
    read=InvoiceOut,
    create=InvoiceIn,
    update=InvoicePatch,
    delete=True,
    access=staff,
    owner="owner_id",
    scope=invoice_scope,
    filters=InvoiceFilters,
    pagination=PageNumberPagination(page_size=20, max_page_size=50),
    links={"tags": tag_scope},
)
class Invoices(API):
    def get_queryset(self, caller: Caller[User]) -> QuerySet[Invoice]:
        _ = caller
        return Invoice.filter(archived=False)

    def filter_queryset(
        self,
        query: QuerySet[Invoice],
        params: InvoiceFilters,
        caller: Caller[User],
    ) -> QuerySet[Invoice]:
        # Intentionally return an unscoped QuerySet so the test proves the
        # framework re-applies ``scope=`` rather than trusting the hook.
        _ = query, params, caller
        return Invoice.all()

    def written_queryset(self, caller: Caller[User]) -> QuerySet[Invoice]:
        _ = caller
        return Invoice.all()

    @http.post(
        "/{item_id}/reopen",
        access=staff,
        atomic=True,
        errors=(errors.NotFound, errors.Forbidden, errors.Conflict),
    )
    async def reopen(self, item_id: UUID, caller: Caller[User]) -> InvoiceOut:
        row = await reopen_invoice(item_id, caller)
        loaded = (
            await invoice_scope.apply(Invoice.all(), caller)
            .filter(id=row.id)
            .prefetch_related(tags=tag_scope.for_caller(caller))
            .aget()
        )
        return InvoiceOut.from_model(loaded)


class PlainTag(Model, table="rs_plain_tags"):
    id: UUID = uuid_pk()
    tenant_id: str
    name: str


class PlainInvoice(Model, table="rs_plain_invoices"):
    id: UUID = uuid_pk()
    tenant_id: str
    owner_id: str
    title: str
    tags: ClassVar[ManyToMany[PlainTag]] = many_to_many(
        PlainTag, ids_name="tag_ids", max_items=5
    )


class PlainOut(Schema):
    id: UUID
    tenant_id: str
    owner_id: str
    title: str
    tag_ids: TagIds


class PlainIn(Schema):
    title: str
    tag_ids: TagIds


class PlainPatch(Schema):
    title: str | Unset = UNSET
    tag_ids: TagIds | Unset = UNSET


class PlainJWTAuth(ClaimsJWTAuth):
    keys = (JWTKey("2026-11", "HS256", JWT_SECRET),)


jwt_access = Access(auth=PlainJWTAuth(), permissions=["invoices:use"])
plain_invoice_scope = RowScope.by_claim(PlainInvoice, "tenant_id", "tenant_id")
plain_tag_scope = RowScope.by_claim(PlainTag, "tenant_id", "tenant_id")
caller_tag_scope = RowScope.by_caller(PlainTag, "tenant_id")


@crud(
    PlainInvoice,
    path="/jwt-scoped",
    read=PlainOut,
    create=PlainIn,
    update=PlainPatch,
    delete=True,
    access=jwt_access,
    owner="owner_id",
    identity_key=caller_id,
    scope=plain_invoice_scope,
    links={"tags": plain_tag_scope},
)
class JwtScopedInvoices(API):
    pass


class CallerOwned(Model, table="rs_caller_owned"):
    id: UUID = uuid_pk()
    account_id: str
    label: str


class CallerOwnedOut(Schema):
    id: UUID
    account_id: str
    label: str


class CallerOwnedIn(Schema):
    label: str


@crud(
    CallerOwned,
    path="/caller-scoped",
    read=CallerOwnedOut,
    create=CallerOwnedIn,
    delete=True,
    access=jwt_access,
    scope=RowScope.by_caller(CallerOwned, "account_id"),
)
class CallerScopedItems(API):
    pass


def make_jwt(claims: dict[str, Any]) -> dict[str, str]:
    payload = {"exp": 4_000_000_000, "scopes": ["invoices:use"], **claims}
    token = core.jwt_encode(
        core.encode_json(payload), ("2026-11", "HS256", JWT_SECRET, False)
    )
    return {"authorization": f"Bearer {token}"}


ALICE = {"authorization": "Bearer alice-token"}
AARON = {"authorization": "Bearer aaron-token"}
BOB = {"authorization": "Bearer bob-token"}


def test_caller_claims_is_immutable_and_isolated() -> None:
    default_caller = Caller(id="u1", user=None)
    assert isinstance(default_caller.claims, MappingProxyType)
    assert default_caller.claims == {}

    source: dict[str, Any] = {"sub": "u1", "tenant_id": "acme"}
    caller = Caller(id="u1", user=None, claims=source)
    source["tenant_id"] = "globex"
    assert caller.claims["tenant_id"] == "acme"
    with pytest.raises(TypeError):
        caller.claims["tenant_id"] = "evil"  # type: ignore[index]

    auth = PlainJWTAuth()
    resolved = auth.resolve_claims({"sub": "alice", "tenant_id": "acme", "scopes": []})
    assert resolved is not None
    assert resolved.claims["tenant_id"] == "acme"


def test_row_scope_constructors_and_registration_errors() -> None:
    assert not hasattr(RowScope, "custom")

    with pytest.raises(RegistrationError, match=r"Model subclass"):
        RowScope.by(cast(Any, object), "tenant_id", from_user("tenant_id"))
    with pytest.raises(RegistrationError, match=r"abstract"):
        RowScope.by_claim(AbstractItem, "tenant_id", "tenant_id")
    with pytest.raises(RegistrationError, match=r"no column"):
        RowScope.by_claim(Invoice, "missing_col", "tenant_id")
    with pytest.raises(RegistrationError, match=r"text column"):
        RowScope.by_caller(Invoice, "archived")
    with pytest.raises(RegistrationError, match=r"non-empty string"):
        RowScope.by_claim(Invoice, "tenant_id", "")
    with pytest.raises(RegistrationError, match=r"callable"):
        RowScope.by(Invoice, "tenant_id", "not-callable")  # type: ignore[arg-type]

    with pytest.raises(RegistrationError, match=r"QuerySet"):
        invoice_scope.apply(Tag.all(), Caller(id="alice", user=USERS["alice-token"]))  # type: ignore[arg-type]

    with pytest.raises(RegistrationError, match=r"RowScope"):
        crud(Invoice, path="/bad", read=InvoiceOut, access=staff, scope="bad")  # type: ignore[arg-type]

    with pytest.raises(RegistrationError, match=r"belongs to"):
        crud(Invoice, path="/bad", read=InvoiceOut, access=staff, scope=tag_scope).api()

    with pytest.raises(RegistrationError, match=r"non-primary-key"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            access=staff,
            scope=RowScope.by_claim(PlainInvoice, "id", "tenant_id"),
        ).api()

    with pytest.raises(RegistrationError, match=r"every active action"):
        crud(
            CallerOwned,
            path="/bad",
            read=CallerOwnedOut,
            scope=RowScope.by_caller(CallerOwned, "account_id"),
        ).api()

    with pytest.raises(RegistrationError, match=r"cannot also be owner"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            access=staff,
            owner="tenant_id",
            scope=plain_invoice_scope,
        ).api()

    with pytest.raises(RegistrationError, match=r"assign= cannot set scope"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            access=staff,
            scope=plain_invoice_scope,
            assign={"tenant_id": from_user("tenant_id")},
        ).api()

    class BadCreate(Schema):
        tenant_id: str
        title: str
        tag_ids: TagIds

    with pytest.raises(RegistrationError, match=r"server assigns"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            create=BadCreate,
            access=staff,
            scope=plain_invoice_scope,
            links={"tags": plain_tag_scope},
        ).api()

    class BadPatch(Schema):
        tenant_id: str | Unset = UNSET

    with pytest.raises(RegistrationError, match=r"server assigns"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            update=BadPatch,
            access=staff,
            scope=plain_invoice_scope,
            links={"tags": plain_tag_scope},
        ).api()

    with pytest.raises(RegistrationError, match=r"belongs to"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            access=staff,
            links={"tags": plain_invoice_scope},
        ).api()

    with pytest.raises(RegistrationError, match=r"access= on every action"):
        crud(
            PlainInvoice,
            path="/bad",
            read=PlainOut,
            links={"tags": caller_tag_scope},
        ).api()


def test_row_scope_value_validation_refuses_before_query() -> None:
    claim_scope = RowScope.by_claim(Invoice, "tenant_id", "tenant_id")
    by_caller = RowScope.by_caller(Invoice, "tenant_id")
    by_user: RowScope[Invoice, User] = RowScope.by(
        Invoice, "tenant_id", from_user("tenant_id")
    )

    for caller in (
        None,
        Caller(id="alice", user=None, claims={}),
        Caller(id="alice", user=None, claims={"tenant_id": ""}),
        Caller(id="alice", user=None, claims={"tenant_id": 42}),
    ):
        with pytest.raises(Forbidden):
            claim_scope.value_for(caller)

    with pytest.raises(Forbidden):
        by_caller.value_for(Caller(id="", user=None))
    with pytest.raises(Forbidden):
        by_user.value_for(cast(Any, Caller(id="alice", user=None)))
    with pytest.raises(Forbidden):
        by_user.value_for(Caller(id="alice", user=User(id="alice", tenant_id="")))


@pytest.fixture
async def env(tmp_path: Path) -> tuple[Hinoki, Path, Tag, Tag, Invoice, Invoice]:
    db_path = tmp_path / "row_scope.sqlite3"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    url = f"sqlite://{db_path}"
    models: list[type[Model]] = [
        Tag,
        Invoice,
        *Invoice.__bridges__,
        PlainTag,
        PlainInvoice,
        *PlainInvoice.__bridges__,
        CallerOwned,
    ]
    plan(models, mig_dir, "initial", dialect="sqlite")
    db = await Database.connect(url, max_size=1, wait=1)
    try:
        await migrator.apply(db, mig_dir)
        with use_database(db):
            acme_tag = await Tag.acreate(tenant_id="acme", name="urgent")
            globex_tag = await Tag.acreate(tenant_id="globex", name="secret-tag")
            acme_seed = await Invoice.acreate(
                tenant_id="acme", owner_id="alice", title="Seed Acme"
            )
            globex_seed = await Invoice.acreate(
                tenant_id="globex", owner_id="bob", title="Seed Globex"
            )
            await PlainTag.acreate(tenant_id="acme", name="plain-acme")
            await PlainTag.acreate(tenant_id="globex", name="plain-globex")
    finally:
        db.close()
    app = Hinoki(
        include=[Invoices, JwtScopedInvoices, CallerScopedItems],
        modules=[DatabaseModule(url)],
    )
    return app, db_path, acme_tag, globex_tag, acme_seed, globex_seed


async def test_two_tenant_crud_manual_and_m2m_scope(
    env: tuple[Hinoki, Path, Tag, Tag, Invoice, Invoice],
) -> None:
    app, db_path, acme_tag, globex_tag, _acme_seed, globex_seed = env
    async with ASGIClient(app) as client:
        # Both tenants already hold active rows; filter_queryset returns raw
        # Invoice.all(), proving the runtime re-applies scope=.
        bob_initial = await client.get("/invoices", query="archived=false", headers=BOB)
        assert bob_initial.status == 200
        assert bob_initial.body["total"] == 1
        assert [item["tenant_id"] for item in bob_initial.body["items"]] == ["globex"]

        created = await client.post(
            "/invoices",
            json={"title": "Q4 Audit", "tag_ids": [str(acme_tag.id)]},
            headers=ALICE,
        )
        assert created.status == 201
        inv_id = created.body["id"]
        assert created.body["tenant_id"] == "acme"
        assert created.body["owner_id"] == "alice"
        assert created.body["tag_ids"] == [str(acme_tag.id)]

        # Linking an out-of-tenant tag fails with 422 unknown_reference.
        bad_link = await client.patch(
            f"/invoices/{inv_id}",
            json={"tag_ids": [str(globex_tag.id)]},
            headers=ALICE,
        )
        assert bad_link.status == 422

        # Cross-tenant bridge row is filtered out on read.
        bridge = Invoice.__bridges__[0]
        owner_col, target_col = (
            c for c in bridge.__columns__ if c != bridge.__primary_key__
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            f"INSERT INTO {bridge.__table__} ({owner_col}, {target_col}) VALUES (?, ?)",  # noqa: S608
            (inv_id, str(globex_tag.id)),
        )
        conn.commit()
        conn.close()
        fetched = await client.get(f"/invoices/{inv_id}", headers=ALICE)
        assert fetched.status == 200
        assert fetched.body["tag_ids"] == [str(acme_tag.id)]

        # Alice's filtered list never leaks Globex rows despite filter_queryset=all().
        alice_list = await client.get(
            "/invoices", query="archived=false&ordering=title", headers=ALICE
        )
        assert alice_list.status == 200
        assert alice_list.body["total"] == 2
        assert {item["tenant_id"] for item in alice_list.body["items"]} == {"acme"}

        # Cross-tenant isolation: Bob cannot read or mutate Alice's invoice, nor
        # Alice Bob's.
        assert (await client.get(f"/invoices/{inv_id}", headers=BOB)).status == 404
        assert (
            await client.patch(
                f"/invoices/{inv_id}", json={"title": "Hacked"}, headers=BOB
            )
        ).status == 404
        assert (await client.delete(f"/invoices/{inv_id}", headers=BOB)).status == 404
        assert (
            await client.get(f"/invoices/{globex_seed.id}", headers=ALICE)
        ).status == 404

        # Same-tenant non-owner (Aaron): read 200, generated write 403, reopen 403.
        assert (await client.get(f"/invoices/{inv_id}", headers=AARON)).status == 200
        assert (
            await client.patch(
                f"/invoices/{inv_id}", json={"title": "Hijacked"}, headers=AARON
            )
        ).status == 403
        assert (await client.delete(f"/invoices/{inv_id}", headers=AARON)).status == 403
        assert (
            await client.post(f"/invoices/{inv_id}/reopen", headers=AARON)
        ).status == 403

        # Archiving removes the row from active retrieve (get_queryset), yet
        # PATCH returns 200 via written_queryset keeping the tenant boundary.
        archived = await client.patch(
            f"/invoices/{inv_id}", json={"archived": True}, headers=ALICE
        )
        assert archived.status == 200
        assert archived.body["archived"] is True
        assert (await client.get(f"/invoices/{inv_id}", headers=ALICE)).status == 404

        # Even after filter_queryset returns raw Invoice.all(), Alice sees only
        # acme rows (never globex).
        scoped_list = await client.get("/invoices", query="search=Q4", headers=ALICE)
        assert scoped_list.status == 200
        assert {item["tenant_id"] for item in scoped_list.body["items"]} == {"acme"}

        # Reopen with Bob (globex) -> 404; Alice -> 200; now back on retrieve.
        assert (
            await client.post(f"/invoices/{inv_id}/reopen", headers=BOB)
        ).status == 404
        reopened = await client.post(f"/invoices/{inv_id}/reopen", headers=ALICE)
        assert reopened.status == 200
        assert reopened.body["archived"] is False
        assert (await client.get(f"/invoices/{inv_id}", headers=ALICE)).status == 200

        # Alice deletes her own invoice -> 204.
        assert (await client.delete(f"/invoices/{inv_id}", headers=ALICE)).status == 204


async def test_wrong_model_hooks_raise_registration_error(tmp_path: Path) -> None:
    db_path = tmp_path / "wrong_hook.sqlite3"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    url = f"sqlite://{db_path}"
    plan([Tag, Invoice, *Invoice.__bridges__], mig_dir, "initial", dialect="sqlite")
    db = await Database.connect(url, max_size=1, wait=1)
    try:
        await migrator.apply(db, mig_dir)
    finally:
        db.close()

    @crud(
        Invoice,
        path="/wrong-get",
        read=InvoiceOut,
        access=staff,
        scope=invoice_scope,
        links={"tags": tag_scope},
    )
    class WrongGet(API):
        async def get_queryset(self, caller: Caller[User]) -> QuerySet[Any]:
            _ = caller
            return Tag.all()

    @crud(
        Invoice,
        path="/wrong-filter",
        read=InvoiceOut,
        access=staff,
        filters=InvoiceFilters,
        scope=invoice_scope,
        links={"tags": tag_scope},
    )
    class WrongFilter(API):
        async def filter_queryset(
            self,
            query: QuerySet[Invoice],
            params: InvoiceFilters,
            caller: Caller[User],
        ) -> QuerySet[Any]:
            _ = query, params, caller
            return Tag.all()

    @crud(
        Invoice,
        path="/wrong-written",
        read=InvoiceOut,
        create=InvoiceIn,
        access=staff,
        owner="owner_id",
        scope=invoice_scope,
        links={"tags": tag_scope},
    )
    class WrongWritten(API):
        async def written_queryset(self, caller: Caller[User]) -> QuerySet[Any]:
            _ = caller
            return Tag.all()

    app = Hinoki(
        include=[WrongGet, WrongFilter, WrongWritten],
        modules=[DatabaseModule(url)],
    )
    async with ASGIClient(app) as client:
        assert (await client.get("/wrong-get", headers=ALICE)).status == 500
        assert (
            await client.get("/wrong-filter", query="search=x", headers=ALICE)
        ).status == 500
        assert (
            await client.post("/wrong-written", json={"title": "bad"}, headers=ALICE)
        ).status == 500

    caller = Caller(id="alice", user=USERS["alice-token"])
    page = PageParams(page=1, page_size=20)
    direct_db = await Database.connect(url, max_size=1, wait=1)
    try:
        with use_database(direct_db):
            with pytest.raises(RegistrationError, match=r"get_queryset"):
                await list_rows(
                    cast(Any, WrongGet()), cast(Any, object()), page, principal=caller
                )
            with pytest.raises(RegistrationError, match=r"filter_queryset"):
                await list_rows(
                    cast(Any, WrongFilter()),
                    cast(Any, object()),
                    page,
                    filters=InvoiceFilters.parse({}),
                    principal=caller,
                )
            with pytest.raises(RegistrationError, match=r"written_queryset"):
                await create_row(
                    cast(Any, WrongWritten()),
                    cast(Any, object()),
                    InvoiceIn(title="bad", tag_ids=set()),
                    principal=caller,
                )
    finally:
        direct_db.close()


def test_rust_crudplan_eligibility_and_sql_templates(
    env: tuple[Hinoki, Path, Tag, Tag, Invoice, Invoice],
) -> None:
    app, _, _, _, _, _ = env
    pipeline = Pipeline(app)
    selections: dict[str, tuple[Any, str]] = {}
    for binding in pipeline.bindings:
        selection, reason = select_crud_plan(binding, pipeline)
        selections[f"{binding.route.method} {binding.route.path}"] = (selection, reason)

    assert selections["GET /invoices"] == (None, "custom_route_or_access_policy")
    for route_key in (
        "GET /jwt-scoped",
        "GET /jwt-scoped/{item_id}",
        "POST /jwt-scoped",
        "PATCH /jwt-scoped/{item_id}",
        "DELETE /jwt-scoped/{item_id}",
        "GET /caller-scoped",
        "POST /caller-scoped",
        "DELETE /caller-scoped/{item_id}",
    ):
        selection, reason = selections[route_key]
        assert reason == "eligible", route_key
        assert selection is not None

    create_plan = selections["POST /jwt-scoped"][0].plan
    update_plan = selections["PATCH /jwt-scoped/{item_id}"][0].plan
    delete_plan = selections["DELETE /jwt-scoped/{item_id}"][0].plan
    list_plan = selections["GET /jwt-scoped"][0].plan
    assert create_plan.scope_column == "tenant_id"
    assert list_plan.scope_column == "tenant_id"

    templates = {(dialect, kind): sql for dialect, kind, sql in update_plan.templates()}
    assert '"tenant_id" =' in templates["postgres", "count"]
    assert '"tenant_id" =' in templates["postgres", "retrieve"]
    assert '"tenant_id" =' in templates["postgres", "read_back"]
    assert '"tenant_id" =' in templates["postgres", "update"]
    assert '"owner_id" =' in templates["postgres", "update"]
    assert '"tenant_id" =' in templates["postgres", "delete"]
    assert '"owner_id" =' in templates["postgres", "delete"]
    assert "`tenant_id` =" in templates["mysql", "update_read_back"]
    assert "`owner_id` =" in templates["mysql", "update_read_back"]

    list_sql, counts = list_plan.list_sql("postgres", 20, 0)
    assert '"tenant_id" =' in list_sql
    assert counts == [20, 0]

    del_templates = {
        (dialect, kind): sql for dialect, kind, sql in delete_plan.templates()
    }
    assert '"tenant_id" =' in del_templates["sqlite", "delete"]
    assert '"owner_id" =' in del_templates["sqlite", "delete"]


async def test_claims_jwt_scoped_routes_parity_and_missing_claim_403(
    env: tuple[Hinoki, Path, Tag, Tag, Invoice, Invoice],
) -> None:
    app, db_path, _, _, _, _ = env
    conn = sqlite3.connect(db_path)
    acme_tag_id = conn.execute(
        "SELECT id FROM rs_plain_tags WHERE tenant_id = ?", ("acme",)
    ).fetchone()[0]
    globex_tag_id = conn.execute(
        "SELECT id FROM rs_plain_tags WHERE tenant_id = ?", ("globex",)
    ).fetchone()[0]
    conn.close()

    alice_jwt = make_jwt({"sub": "alice", "tenant_id": "acme"})
    aaron_jwt = make_jwt({"sub": "aaron", "tenant_id": "acme"})
    bob_jwt = make_jwt({"sub": "bob", "tenant_id": "globex"})
    missing_claim = make_jwt({"sub": "alice"})
    empty_claim = make_jwt({"sub": "alice", "tenant_id": ""})
    wrong_type_claim = make_jwt({"sub": "alice", "tenant_id": 123})

    async with ASGIClient(app) as client:
        for bad in (missing_claim, empty_claim, wrong_type_claim):
            assert (await client.get("/jwt-scoped", headers=bad)).status == 403
            assert (
                await client.post(
                    "/jwt-scoped",
                    json={"title": "x", "tag_ids": [acme_tag_id]},
                    headers=bad,
                )
            ).status == 403

        created = await client.post(
            "/jwt-scoped",
            json={"title": "JWT Row", "tag_ids": [acme_tag_id]},
            headers=alice_jwt,
        )
        assert created.status == 201
        row_id = created.body["id"]
        assert created.body["tenant_id"] == "acme"
        assert created.body["owner_id"] == "alice"
        assert created.body["tag_ids"] == [acme_tag_id]

        # Bob in globex sees 0 acme rows and gets 404 on single-row operations.
        assert (await client.get("/jwt-scoped", headers=bob_jwt)).body["total"] == 0
        assert (
            await client.get(f"/jwt-scoped/{row_id}", headers=bob_jwt)
        ).status == 404
        assert (
            await client.patch(
                f"/jwt-scoped/{row_id}", json={"title": "no"}, headers=bob_jwt
            )
        ).status == 404

        # Cross-tenant tag rejected cleanly (422).
        assert (
            await client.patch(
                f"/jwt-scoped/{row_id}",
                json={"tag_ids": [globex_tag_id]},
                headers=alice_jwt,
            )
        ).status == 422

        # Aaron in acme reads 200, writes 403.
        assert (
            await client.get(f"/jwt-scoped/{row_id}", headers=aaron_jwt)
        ).status == 200
        assert (
            await client.patch(
                f"/jwt-scoped/{row_id}", json={"title": "no"}, headers=aaron_jwt
            )
        ).status == 403
        assert (
            await client.delete(f"/jwt-scoped/{row_id}", headers=aaron_jwt)
        ).status == 403

        # RowScope.by_caller scopes strictly by sub.
        c1 = await client.post(
            "/caller-scoped", json={"label": "mine"}, headers=alice_jwt
        )
        assert c1.status == 201
        assert c1.body["account_id"] == "alice"
        assert (await client.get("/caller-scoped", headers=aaron_jwt)).body[
            "total"
        ] == 0
        assert (
            await client.delete(f"/caller-scoped/{c1.body['id']}", headers=alice_jwt)
        ).status == 204


async def test_link_queryset_override_keeps_declared_row_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "link_override.sqlite3"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    url = f"sqlite://{db_path}"
    plan([Tag, Invoice, *Invoice.__bridges__], mig_dir, "initial", dialect="sqlite")
    db = await Database.connect(url, max_size=1, wait=1)
    try:
        await migrator.apply(db, mig_dir)
        with use_database(db):
            acme_tag = await Tag.acreate(tenant_id="acme", name="acme-tag")
            globex_tag = await Tag.acreate(tenant_id="globex", name="globex-tag")
    finally:
        db.close()

    @crud(
        Invoice,
        path="/link-override",
        read=InvoiceOut,
        create=InvoiceIn,
        update=InvoicePatch,
        access=staff,
        owner="owner_id",
        scope=invoice_scope,
        links={"tags": tag_scope},
    )
    class LinkOverrideInvoices(API):
        async def link_queryset(
            self, relation: str, principal: Caller[User] | None
        ) -> QuerySet[Any]:
            # Intentionally return an unscoped QuerySet so the test proves the
            # framework re-applies link_scopes after the override.
            _ = relation, principal
            return Tag.all()

    @crud(
        Invoice,
        path="/wrong-link-model",
        read=InvoiceOut,
        create=InvoiceIn,
        access=staff,
        owner="owner_id",
        scope=invoice_scope,
        links={"tags": tag_scope},
    )
    class WrongLinkModel(API):
        async def link_queryset(
            self, relation: str, principal: Caller[User] | None
        ) -> QuerySet[Any]:
            _ = relation, principal
            return Invoice.all()

    app = Hinoki(
        include=[LinkOverrideInvoices, WrongLinkModel],
        modules=[DatabaseModule(url)],
    )
    async with ASGIClient(app) as client:
        # Write path still rejects cross-tenant tag despite Tag.all() hook.
        bad_create = await client.post(
            "/link-override",
            json={"title": "Denied", "tag_ids": [str(globex_tag.id)]},
            headers=ALICE,
        )
        assert bad_create.status == 422

        created = await client.post(
            "/link-override",
            json={"title": "Allowed", "tag_ids": [str(acme_tag.id)]},
            headers=ALICE,
        )
        assert created.status == 201
        inv_id = created.body["id"]

        bad_patch = await client.patch(
            f"/link-override/{inv_id}",
            json={"tag_ids": [str(globex_tag.id)]},
            headers=ALICE,
        )
        assert bad_patch.status == 422

        # Direct cross-tenant bridge row is filtered out on read too.
        bridge = Invoice.__bridges__[0]
        owner_col, target_col = (
            c for c in bridge.__columns__ if c != bridge.__primary_key__
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            f"INSERT INTO {bridge.__table__} ({owner_col}, {target_col}) VALUES (?, ?)",  # noqa: S608
            (inv_id, str(globex_tag.id)),
        )
        conn.commit()
        conn.close()

        fetched = await client.get(f"/link-override/{inv_id}", headers=ALICE)
        assert fetched.status == 200
        assert fetched.body["tag_ids"] == [str(acme_tag.id)]
        listed = await client.get("/link-override", headers=ALICE)
        assert listed.status == 200
        assert listed.body["items"][0]["tag_ids"] == [str(acme_tag.id)]

        # Wrong model returned by link_queryset fails loudly.
        assert (await client.get("/wrong-link-model", headers=ALICE)).status == 500

    caller = Caller(id="alice", user=USERS["alice-token"])
    direct_db = await Database.connect(url, max_size=1, wait=1)
    try:
        with use_database(direct_db):
            with pytest.raises(RegistrationError, match=r"link_queryset"):
                await retrieve_row(
                    cast(Any, WrongLinkModel()),
                    cast(Any, object()),
                    UUID(inv_id),
                    principal=caller,
                )
            with pytest.raises(RegistrationError, match=r"link_queryset"):
                await create_row(
                    cast(Any, WrongLinkModel()),
                    cast(Any, object()),
                    InvoiceIn(title="bad", tag_ids={acme_tag.id}),
                    principal=caller,
                )
    finally:
        direct_db.close()


async def _kiso_http(
    port: int,
    method: str,
    target: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    extra = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    content_type = (
        "content-type: application/json\r\n"
        if body or method in {"POST", "PATCH"}
        else ""
    )
    head = (
        f"{method} {target} HTTP/1.1\r\n"
        "host: test\r\n"
        "x-request-id: 00000000000000000000000000000001\r\n"
        f"{content_type}"
        f"content-length: {len(body)}\r\n"
        f"{extra}\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()
    raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    status = int(raw_head.decode("latin-1").split("\r\n")[0].split()[1])
    length = 0
    for line in raw_head.decode("latin-1").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    payload = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
    writer.close()
    await writer.wait_closed()
    return status, payload


def test_server_core_preserves_verified_claims_on_native_principal(
    tmp_path: Path,
) -> None:
    db_url = f"sqlite://{tmp_path / 'native_claims_core.sqlite3'}"

    @http.get("/manual-claims", access=jwt_access)
    async def manual_claims(caller: Caller[None]) -> dict[str, Any]:
        return {"id": caller.id, "claims": dict(caller.claims)}

    app = Hinoki(include=[manual_claims], modules=[DatabaseModule(db_url)])
    pipeline = Pipeline(app)
    assert native_handler_eligible(pipeline.bindings[0])
    server_core = build_server_core(pipeline)
    server_core.set_ready(True)
    auth_header = make_jwt({"sub": "alice", "tenant_id": "acme"})["authorization"]
    _submission = server_core.submit(
        "GET", "/manual-claims", b"", [(b"authorization", auth_header.encode())]
    )
    [prepared] = server_core.drain(1)
    assert prepared.native_handler is True
    assert prepared.native_principal is not None
    subject, scopes, claims = prepared.native_principal
    assert subject == "alice"
    assert scopes == ["invoices:use"]
    assert claims["tenant_id"] == "acme"


async def test_kiso_native_handler_lane_preserves_caller_claims_and_row_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kiso_manual_claims.sqlite3"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    url = f"sqlite://{db_path}"
    plan([PlainTag], mig_dir, "initial", dialect="sqlite")
    db = await Database.connect(url, max_size=1, wait=1)
    try:
        await migrator.apply(db, mig_dir)
        with use_database(db):
            await PlainTag.acreate(tenant_id="acme", name="acme-only")
            await PlainTag.acreate(tenant_id="globex", name="globex-only")
    finally:
        db.close()

    @http.get("/manual-scoped-tags", access=jwt_access)
    async def manual_scoped_tags(caller: Caller[None]) -> dict[str, Any]:
        rows = await plain_tag_scope.for_caller(caller).order_by("name")
        return {
            "tenant_id": caller.claims.get("tenant_id"),
            "names": [row.name for row in rows],
        }

    app = Hinoki(include=[manual_scoped_tags], modules=[DatabaseModule(url)])
    pipeline = Pipeline(app)
    assert native_handler_eligible(pipeline.bindings[0])

    alice_jwt = make_jwt({"sub": "alice", "tenant_id": "acme"})
    missing_jwt = make_jwt({"sub": "alice"})

    async with ASGIClient(app) as client:
        ok_asgi = await client.get("/manual-scoped-tags", headers=alice_jwt)
        assert ok_asgi.status == 200
        assert ok_asgi.body == {"tenant_id": "acme", "names": ["acme-only"]}
        assert (
            await client.get("/manual-scoped-tags", headers=missing_jwt)
        ).status == 403

    worker = KisoWorker(app, "127.0.0.1", 0)
    await worker.start()
    try:
        port = worker.address[1]
        status, body = await _kiso_http(
            port, "GET", "/manual-scoped-tags", headers=alice_jwt
        )
        assert status == 200
        assert json.loads(body) == {"tenant_id": "acme", "names": ["acme-only"]}
        miss_status, _ = await _kiso_http(
            port, "GET", "/manual-scoped-tags", headers=missing_jwt
        )
        assert miss_status == 403
    finally:
        await worker.stop()


async def _scoped_crud_kiso_scenario(
    url: str, *, native: bool, acme_tag_id: str, globex_tag_id: str
) -> tuple[list[tuple[str, int, str]], int]:
    app = Hinoki(include=[JwtScopedInvoices], modules=[DatabaseModule(url)])
    worker = KisoWorker(app, "127.0.0.1", 0)
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
        callers = {
            "alice": make_jwt({"sub": "alice", "tenant_id": "acme"}),
            "aaron": make_jwt({"sub": "aaron", "tenant_id": "acme"}),
            "bob": make_jwt({"sub": "bob", "tenant_id": "globex"}),
        }
        bad_claims = (
            ("missing", make_jwt({"sub": "alice"})),
            ("empty", make_jwt({"sub": "alice", "tenant_id": ""})),
            ("wrong_type", make_jwt({"sub": "alice", "tenant_id": 123})),
        )
        alice, aaron, bob = callers["alice"], callers["aaron"], callers["bob"]

        row_id = ""
        log: list[tuple[str, int, str]] = []

        async def call(
            label: str,
            method: str,
            target: str,
            headers: dict[str, str],
            payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            resolved = target.replace("<id>", row_id)
            raw_body = json.dumps(payload).encode() if payload is not None else b""
            status, content = await _kiso_http(
                port, method, resolved, headers=headers, body=raw_body
            )
            text = re.sub(
                rb'"request_id":"[0-9a-f]+"', b'"request_id":""', content
            ).decode()
            if row_id:
                text = text.replace(row_id, "<id>")
            log.append((label, status, text))
            return json.loads(content) if content else {}

        for tag_label, bad_headers in bad_claims:
            await call(f"list_{tag_label}", "GET", "/jwt-scoped", bad_headers)
            await call(
                f"create_{tag_label}",
                "POST",
                "/jwt-scoped",
                bad_headers,
                {"title": "denied", "tag_ids": [acme_tag_id]},
            )

        created = await call(
            "alice_create",
            "POST",
            "/jwt-scoped",
            alice,
            {"title": "Kiso Scoped", "tag_ids": [acme_tag_id]},
        )
        row_id = str(created["id"])
        # Normalize create log entry with `<id>` substituted now that row_id is known.
        label, status, raw_text = log[-1]
        log[-1] = (label, status, raw_text.replace(row_id, "<id>"))

        steps = (
            ("alice_list", "GET", "/jwt-scoped", alice, None),
            ("bob_list", "GET", "/jwt-scoped", bob, None),
            ("alice_retrieve", "GET", "/jwt-scoped/<id>", alice, None),
            ("bob_retrieve", "GET", "/jwt-scoped/<id>", bob, None),
            ("aaron_retrieve", "GET", "/jwt-scoped/<id>", aaron, None),
            (
                "alice_bad_tag",
                "PATCH",
                "/jwt-scoped/<id>",
                alice,
                {"tag_ids": [globex_tag_id]},
            ),
            ("aaron_patch", "PATCH", "/jwt-scoped/<id>", aaron, {"title": "hijack"}),
            ("bob_patch", "PATCH", "/jwt-scoped/<id>", bob, {"title": "hijack"}),
            (
                "alice_patch",
                "PATCH",
                "/jwt-scoped/<id>",
                alice,
                {"title": "Updated Scoped"},
            ),
            ("aaron_delete", "DELETE", "/jwt-scoped/<id>", aaron, None),
            ("bob_delete", "DELETE", "/jwt-scoped/<id>", bob, None),
            ("alice_delete", "DELETE", "/jwt-scoped/<id>", alice, None),
            ("after_delete", "GET", "/jwt-scoped/<id>", alice, None),
        )
        for step_label, method, target, headers, payload in steps:
            await call(step_label, method, target, headers, payload)
    finally:
        await worker.stop()
    return log, python_requests


async def test_kiso_scoped_crud_native_vs_python_parity(tmp_path: Path) -> None:
    logs: list[list[tuple[str, int, str]]] = []
    python_counts: list[int] = []
    for native in (True, False):
        db_path = tmp_path / f"kiso_parity_{native}.sqlite3"
        url = f"sqlite://{db_path}"
        database = await Database.connect(url, max_size=1, wait=1)
        models: tuple[type[Model], ...] = (
            PlainTag,
            PlainInvoice,
            *PlainInvoice.__bridges__,
        )
        try:
            for model in models:
                await database.execute(f"DROP TABLE IF EXISTS {model.__table__}")
                await database.execute(
                    create_table_sql(model.__table__, table_state(model), "sqlite")
                )
            with use_database(database):
                acme_tag = await PlainTag.acreate(
                    id=UUID(int=1), tenant_id="acme", name="a"
                )
                globex_tag = await PlainTag.acreate(
                    id=UUID(int=2), tenant_id="globex", name="g"
                )
        finally:
            database.close()
        log, py_count = await _scoped_crud_kiso_scenario(
            url,
            native=native,
            acme_tag_id=str(acme_tag.id),
            globex_tag_id=str(globex_tag.id),
        )
        logs.append(log)
        python_counts.append(py_count)

    native_log, python_log = logs
    assert native_log == python_log
    assert python_counts[0] == 0
    assert python_counts[1] == len(native_log)


def _build_manual_patch_app(url: str) -> Hinoki:
    @crud(
        Invoice,
        path="/manual-patch-invoices",
        read=InvoiceOut,
        create=InvoiceIn,
        update=InvoicePatch,
        delete=True,
        access=staff,
        owner="owner_id",
        scope=invoice_scope,
        links={"tags": tag_scope},
        omit=("update",),
    )
    class ManualPatchInvoices(API):
        def get_queryset(self, caller: Caller[User]) -> QuerySet[Invoice]:
            _ = caller
            return Invoice.filter(archived=False)

        @http.patch(
            "/{item_id}",
            access=staff,
            atomic=True,
            errors=(errors.NotFound, errors.Forbidden, errors.BadRequest),
        )
        async def update(
            self, item_id: UUID, data: InvoicePatch, caller: Caller[User]
        ) -> InvoiceOut:
            row = (
                await invoice_scope.apply(Invoice.all(), caller)
                .filter(id=item_id)
                .aget()
            )
            if row.owner_id != caller.id:
                raise errors.Forbidden("You cannot change this item.")
            await _apply_manual_invoice_patch(item_id, row, data, caller)
            loaded = (
                await invoice_scope.apply(Invoice.all(), caller)
                .filter(id=item_id)
                .prefetch_related(tags=tag_scope.for_caller(caller))
                .aget()
            )
            return InvoiceOut.from_model(loaded)

    class IgnoredBadPatch(Schema):
        title: str = "default-not-unset"

    @crud(
        PlainInvoice,
        path="/jwt-partial",
        read=PlainOut,
        create=PlainIn,
        update=IgnoredBadPatch,
        delete=True,
        access=jwt_access,
        owner="owner_id",
        identity_key=caller_id,
        scope=plain_invoice_scope,
        links={"tags": plain_tag_scope},
        omit=("update",),
    )
    class JwtPartialNative(API):
        @http.patch(
            "/{item_id}",
            access=jwt_access,
            atomic=True,
            errors=(errors.NotFound, errors.Forbidden),
        )
        async def update(
            self, item_id: UUID, data: PlainPatch, caller: Caller[None]
        ) -> PlainOut:
            row = await plain_invoice_scope.for_caller(caller).filter(id=item_id).aget()
            if row.owner_id != caller.id:
                raise errors.Forbidden("You cannot change this item.")
            if not isinstance(data.title, Unset):
                await PlainInvoice.aupdate_where(
                    {
                        "id": item_id,
                        "tenant_id": plain_invoice_scope.value_for(caller),
                        "owner_id": caller.id,
                    },
                    {"title": data.title},
                )
            loaded = (
                await plain_invoice_scope.for_caller(caller)
                .filter(id=item_id)
                .prefetch_related(tags=plain_tag_scope.for_caller(caller))
                .aget()
            )
            return PlainOut.from_model(loaded)

    return Hinoki(
        include=[ManualPatchInvoices, JwtPartialNative],
        modules=[DatabaseModule(url)],
    )


async def _apply_manual_invoice_patch(
    item_id: UUID, row: Invoice, data: InvoicePatch, caller: Caller[User]
) -> None:
    updates: dict[str, Any] = {}
    if not isinstance(data.title, Unset):
        updates["title"] = data.title
    if not isinstance(data.archived, Unset):
        updates["archived"] = data.archived
    if updates:
        await Invoice.aupdate_where(
            {
                "id": item_id,
                "tenant_id": invoice_scope.value_for(caller),
                "owner_id": caller.id,
            },
            updates,
        )
    if not isinstance(data.tag_ids, Unset):
        allowed = await tag_scope.for_caller(caller).filter(id__in=list(data.tag_ids))
        if len(allowed) != len(data.tag_ids):
            raise errors.ValidationError(
                [errors.ErrorDetail("tag_ids", "unknown_reference", "invalid")]
            )
        await row.tags.aset(allowed)
    if updates.get("title") == "ROLLBACK":
        raise errors.BadRequest("trigger transaction rollback")


async def _verify_manual_patch_http(
    client: ASGIClient, acme_tag: Tag, globex_tag: Tag, plain_acme_tag: PlainTag
) -> None:
    created = await client.post(
        "/manual-patch-invoices",
        json={"title": "Initial", "tag_ids": [str(acme_tag.id)]},
        headers=ALICE,
    )
    assert created.status == 201
    inv_id = created.body["id"]

    # Generated list still isolates tenants.
    bob_list = await client.get("/manual-patch-invoices", headers=BOB)
    assert bob_list.status == 200
    assert [row["title"] for row in bob_list.body["items"]] == ["Globex Seed"]

    # Manual PATCH enforces tenant scope (404), owner (403), and M2M scope (422).
    assert (
        await client.patch(
            f"/manual-patch-invoices/{inv_id}", json={"title": "Steal"}, headers=BOB
        )
    ).status == 404
    assert (
        await client.patch(
            f"/manual-patch-invoices/{inv_id}", json={"title": "Hijack"}, headers=AARON
        )
    ).status == 403
    assert (
        await client.patch(
            f"/manual-patch-invoices/{inv_id}",
            json={"tag_ids": [str(globex_tag.id)]},
            headers=ALICE,
        )
    ).status == 422

    # Manual PATCH runs inside atomic=True and rolls back on failure.
    failed_tx = await client.patch(
        f"/manual-patch-invoices/{inv_id}", json={"title": "ROLLBACK"}, headers=ALICE
    )
    assert failed_tx.status == 400
    after_rollback = await client.get(f"/manual-patch-invoices/{inv_id}", headers=ALICE)
    assert after_rollback.status == 200
    assert after_rollback.body["title"] == "Initial"

    # Valid manual PATCH updates title and returns scoped M2M tags.
    updated = await client.patch(
        f"/manual-patch-invoices/{inv_id}",
        json={"title": "Manually Updated", "tag_ids": [str(acme_tag.id)]},
        headers=ALICE,
    )
    assert updated.status == 200
    assert updated.body["title"] == "Manually Updated"
    assert updated.body["tag_ids"] == [str(acme_tag.id)]
    assert (
        await client.delete(f"/manual-patch-invoices/{inv_id}", headers=ALICE)
    ).status == 204

    # JWT resource keeps generated reads/writes + manual PATCH working together.
    alice_jwt = make_jwt({"sub": "alice", "tenant_id": "acme"})
    bob_jwt = make_jwt({"sub": "bob", "tenant_id": "globex"})
    jwt_created = await client.post(
        "/jwt-partial",
        json={"title": "JWT Draft", "tag_ids": [str(plain_acme_tag.id)]},
        headers=alice_jwt,
    )
    assert jwt_created.status == 201
    jwt_id = jwt_created.body["id"]
    assert (
        await client.patch(
            f"/jwt-partial/{jwt_id}", json={"title": "Cross"}, headers=bob_jwt
        )
    ).status == 404
    jwt_patched = await client.patch(
        f"/jwt-partial/{jwt_id}", json={"title": "JWT Patched"}, headers=alice_jwt
    )
    assert jwt_patched.status == 200
    assert jwt_patched.body["title"] == "JWT Patched"
    assert (
        await client.delete(f"/jwt-partial/{jwt_id}", headers=alice_jwt)
    ).status == 204


async def test_two_tenant_scoped_crud_replaces_only_update_with_manual_patch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "manual_patch_transition.sqlite3"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    url = f"sqlite://{db_path}"
    models: tuple[type[Model], ...] = (
        Tag,
        Invoice,
        *Invoice.__bridges__,
        PlainTag,
        PlainInvoice,
        *PlainInvoice.__bridges__,
    )
    plan(list(models), mig_dir, "initial", dialect="sqlite")
    db = await Database.connect(url, max_size=1, wait=1)
    try:
        await migrator.apply(db, mig_dir)
        with use_database(db):
            acme_tag = await Tag.acreate(tenant_id="acme", name="urgent")
            globex_tag = await Tag.acreate(tenant_id="globex", name="secret")
            await Invoice.acreate(
                tenant_id="globex", owner_id="bob", title="Globex Seed"
            )
            plain_acme_tag = await PlainTag.acreate(tenant_id="acme", name="plain-a")
            await PlainTag.acreate(tenant_id="globex", name="plain-g")
    finally:
        db.close()

    app = _build_manual_patch_app(url)
    pipeline = Pipeline(app)
    jwt_bindings = {
        (b.route.method, b.route.path): b
        for b in pipeline.bindings
        if b.route.path.startswith("/jwt-partial")
    }
    for action_key in (
        ("GET", "/jwt-partial"),
        ("GET", "/jwt-partial/{item_id}"),
        ("POST", "/jwt-partial"),
        ("DELETE", "/jwt-partial/{item_id}"),
    ):
        selection, reason = select_crud_plan(jwt_bindings[action_key], pipeline)
        assert reason == "eligible", action_key
        assert selection is not None
    patch_selection, _ = select_crud_plan(
        jwt_bindings[("PATCH", "/jwt-partial/{item_id}")], pipeline
    )
    assert patch_selection is None

    async with ASGIClient(app) as client:
        await _verify_manual_patch_http(client, acme_tag, globex_tag, plain_acme_tag)
