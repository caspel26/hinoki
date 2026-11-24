"""Admin defaults and authorization for soft-deleted models."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from urllib.parse import urlencode

import pytest

from hinoki import db
from hinoki.admin.components import Components
from hinoki.admin.lists import bulk_controls, table_rows
from hinoki.admin.modeladmin import BulkAction, ModelAdmin
from hinoki.admin.site import ListState, Site
from hinoki.admin.templates import Templates
from hinoki.admin.views import (
    authorize_bulk_rows,
    authorized_bulk_action,
    bulk_confirmation,
    finish_bulk,
    run_bulk_action,
)
from hinoki.auth import Caller
from hinoki.db import Database
from hinoki.http.response import Response
from hinoki.orm import Model, OnDelete, field, foreign_key, pk, use_database


def fake_site(state: ListState, principal: Caller[Any]) -> Site:
    async def page(_title: str, content: Any, *, active: str = "") -> Response:
        return Response(200, str(content))

    def url(*parts: str, **query: str) -> str:
        suffix = urlencode({key: value for key, value in query.items() if value})
        path = "/admin/" + "/".join(parts)
        return f"{path}?{suffix}" if suffix else path

    async def problem(status: int, message: str) -> Response:
        return Response(status, message)

    return cast(
        Site,
        SimpleNamespace(
            admin=SimpleNamespace(templates=Templates(), models={}),
            principal=principal,
            state=state,
            token="x",  # noqa: S106 - synthetic request token
            parts=Components(Templates()),
            url=url,
            page=page,
            problem=problem,
        ),
    )


@db.track_status("status", changed_at="status_changed_at")
@db.soft_delete()
class AuditedRow(Model, table="admin_soft_deleted_rows"):
    id: int = pk()
    owner: str
    status: str = "open"
    status_changed_at: datetime
    deleted_at: datetime | None = field(default=None, index=True)


class ScopedAdmin(ModelAdmin[AuditedRow]):
    allow_hard_delete = False

    def deleted_queryset(self, principal: Caller[Any]) -> Any:
        return self.model.only_deleted().filter(owner=principal.id)

    def can(
        self, principal: Caller[Any], action: str, row: AuditedRow | None = None
    ) -> bool:
        return action != "delete" or "purge" in principal.permissions


class PurgingAdmin(ScopedAdmin):
    allow_hard_delete = True


class DependentRow(Model, table="admin_soft_delete_dependents"):
    id: int = pk()
    row_id: int = foreign_key(AuditedRow, on_delete=OnDelete.RESTRICT)


async def custom_bulk_action(
    _rows: tuple[AuditedRow, ...], _principal: Caller[Any]
) -> None:
    return None


class CustomActionAdmin(ScopedAdmin):
    bulk_actions: ClassVar[tuple[BulkAction[Any], ...]] = (
        BulkAction("custom", "Custom", "Run custom?", custom_bulk_action),
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    opened = await Database.connect(
        f"sqlite://{tmp_path / 'admin-soft-delete.sqlite3'}"
    )
    await opened.execute(
        "CREATE TABLE admin_soft_deleted_rows (id integer PRIMARY KEY, "
        "owner text NOT NULL, status text NOT NULL, status_changed_at text NOT NULL, "
        "deleted_at text)"
    )
    await opened.execute(
        "CREATE TABLE admin_soft_delete_dependents (id integer PRIMARY KEY, "
        "row_id integer NOT NULL REFERENCES admin_soft_deleted_rows(id) "
        "ON DELETE RESTRICT)"
    )
    try:
        with use_database(opened):
            yield opened
    finally:
        opened.close()


def test_managed_columns_become_readonly_and_deleted_scope_is_narrowed() -> None:
    entry = ScopedAdmin()
    entry.normalize("audited-rows")
    assert entry.readonly == ("deleted_at", "status_changed_at")

    principal = Caller(id="owner-a", user="owner-a")
    query = entry.deleted_scope(principal)
    assert ("owner", "eq", ["owner-a"]) in query.filters
    assert ("deleted_at", "notnull", []) in query.filters


def test_restore_and_hard_delete_bulk_actions_are_fail_closed() -> None:
    entry = ScopedAdmin()
    principal = Caller(id="owner-a", user="owner-a")
    assert authorized_bulk_action(entry, principal, "restore") == (False, None)
    assert authorized_bulk_action(entry, principal, "hard-delete") is None

    row = AuditedRow(id=1, owner="owner-a", status_changed_at=datetime.now())
    assert authorize_bulk_rows(entry, principal, [1], [row], "restore") == [row]
    assert authorize_bulk_rows(entry, principal, [1], [row], "hard-delete") is None


def test_hard_delete_requires_opt_in_and_row_permission() -> None:
    principal = Caller(id="owner-a", user="owner-a", permissions={"purge"})
    entry = PurgingAdmin()
    row = AuditedRow(id=1, owner="owner-a", status_changed_at=datetime.now())
    assert authorized_bulk_action(entry, principal, "hard-delete") == (False, None)
    assert authorize_bulk_rows(entry, principal, [1], [row], "hard-delete") == [row]


async def test_admin_bulk_actions_keep_the_overridden_deleted_scope(
    database: Database,
) -> None:
    principal = Caller(id="owner-a", user="owner-a", permissions={"purge"})
    row = await AuditedRow.acreate(
        id=10, owner="owner-a", status_changed_at=datetime(2026, 11, 24, tzinfo=UTC)
    )
    await row.asoft_delete()
    entry = ScopedAdmin()
    assert authorized_bulk_action(entry, principal, "restore") == (False, None)
    await run_bulk_action(entry, [row], principal, None, "restore")
    assert not (await AuditedRow.aget(id=10)).is_deleted

    outside = await AuditedRow.acreate(
        id=11, owner="owner-b", status_changed_at=datetime(2026, 11, 24, tzinfo=UTC)
    )
    await outside.asoft_delete()
    await run_bulk_action(entry, [outside], principal, None, "restore")
    assert (await AuditedRow.only_deleted().filter(id=11).aget()).is_deleted

    purge = await AuditedRow.acreate(
        id=12, owner="owner-a", status_changed_at=datetime(2026, 11, 24, tzinfo=UTC)
    )
    await purge.asoft_delete()
    purge_admin = PurgingAdmin()
    assert authorized_bulk_action(purge_admin, principal, "hard-delete") == (
        False,
        None,
    )
    await run_bulk_action(purge_admin, [purge], principal, None, "hard-delete")
    assert not await AuditedRow.with_deleted().filter(id=12).aexists()


async def test_admin_restrict_hard_delete_becomes_conflict_and_keeps_deleted_view(
    database: Database,
) -> None:
    principal = Caller(id="owner-a", user="owner-a", permissions={"purge"})
    row = await AuditedRow.acreate(
        id=20, owner="owner-a", status_changed_at=datetime(2026, 11, 24, tzinfo=UTC)
    )
    await row.asoft_delete()
    await DependentRow.acreate(id=1, row_id=row.id)
    site: Any = fake_site(ListState(deleted=True), principal)
    site.request = SimpleNamespace(POST={"confirmed": "1"})
    result = await finish_bulk(
        site, PurgingAdmin(), "audited-rows", ["20"], "hard-delete", None, False
    )
    assert result.status == 409
    assert "database rejected" in str(result.body)


async def test_deleted_view_markup_query_and_notice_behavior() -> None:
    principal = Caller(id="owner-a", user="owner-a", permissions={"purge"})
    entry = CustomActionAdmin()
    entry.normalize("audited-rows")
    row = AuditedRow(
        id=31,
        owner="owner-a",
        status="closed",
        status_changed_at=datetime.now(UTC),
        deleted_at=datetime.now(UTC),
    )
    site: Any = fake_site(ListState(deleted=True), principal)
    site.action_done_notice = lambda: Site.action_done_notice(site)
    assert "hn-rowlink" not in str(
        table_rows(site, "audited-rows", entry, [row], bulk=False)
    )
    _delete, actions = bulk_controls(site, entry)
    assert [name for name, _label in actions] == ["restore"]

    confirmation = await bulk_confirmation(
        site, "audited-rows", entry, [row], "restore", None
    )
    assert 'action="/admin/audited-rows/bulk?deleted=1"' in str(confirmation.body)
    assert 'href="/admin/audited-rows?deleted=1"' in str(confirmation.body)

    site.admin.models = {"audited-rows": entry}
    for action, expected in (
        ("restore", "Restored."),
        ("hard-delete", "Permanently deleted."),
    ):
        site.state = ListState(deleted=True, done=f"action:audited-rows:{action}")
        assert expected in str(Site.done_notice(site))


async def test_restore_redirect_preserves_deleted_view_and_notice(
    database: Database,
) -> None:
    principal = Caller(id="owner-a", user="owner-a")
    row = await AuditedRow.acreate(
        id=40, owner="owner-a", status_changed_at=datetime(2026, 11, 24, tzinfo=UTC)
    )
    await row.asoft_delete()
    site: Any = fake_site(ListState(deleted=True), principal)
    site.request = SimpleNamespace(POST={"confirmed": "1"})
    response = await finish_bulk(
        site, ScopedAdmin(), "audited-rows", ["40"], "restore", None, False
    )
    assert response.status == 303
    assert response.headers[0] == (
        b"location",
        b"/admin/audited-rows?done=action%3Aaudited-rows%3Arestore&deleted=1",
    )


async def test_empty_bulk_cancel_redirect_preserves_deleted_query() -> None:
    principal = Caller(id="owner-a", user="owner-a")
    site = fake_site(ListState(deleted=True), principal)
    response = await finish_bulk(
        site, ScopedAdmin(), "audited-rows", [], "restore", None, False
    )
    assert response.headers[0] == (b"location", b"/admin/audited-rows?deleted=1")
