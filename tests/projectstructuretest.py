"""Acceptance tests for D5 project structure, explicit mounting and mixed apps."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from hinoki import Hinoki
from hinoki.__main__ import main
from hinoki.db import Database
from hinoki.errors import RegistrationError
from hinoki.orm import migrator
from hinoki.project import ProjectError, load_manifest
from hinoki.scaffold import create
from hinoki.testing import ASGIClient


def _clear_package(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    for name in [
        key for key in sys.modules if key == prefix or key.startswith(f"{prefix}.")
    ]:
        monkeypatch.delitem(sys.modules, name)


def _get(app: Hinoki, path: str, *, bearer: str | None = None) -> tuple[int, object]:
    async def run() -> tuple[int, object]:
        async with ASGIClient(app) as client:
            reply = await client.get(path, token=bearer)
        return reply.status, reply.body

    return asyncio.run(run())


def test_minimal_project_without_db_serves_health_and_respects_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        main(
            [
                "new",
                "minproj",
                "--database",
                "none",
                "--no-app",
                "--no-input",
                "--no-agent-files",
                "--dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    project = tmp_path / "minproj"
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    _clear_package(monkeypatch, "minproj")

    app = importlib.import_module("minproj.main").app
    assert [route.path for route in app.routes] == ["/health"]
    assert _get(app, "/health") == (200, {"status": "ok"})


def test_persistent_mounted_and_unmounted_apps_in_same_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = create("shopproj", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    _clear_package(monkeypatch, "shopproj")

    assert main(["app", "create", "catalog", "--no-input"]) == 0
    assert main(["app", "create", "ledger", "--no-input"]) == 0
    capsys.readouterr()

    catalog_dir = project / "shopproj" / "apps" / "catalog"
    ledger_dir = project / "shopproj" / "apps" / "ledger"
    (catalog_dir / "models.py").write_text(
        "from uuid import UUID, uuid4\n"
        "from hinoki.orm import Model, field\n\n"
        'class Item(Model, table="catalog_items"):\n'
        "    id: UUID = field(primary_key=True, default_factory=uuid4)\n"
        "    name: str\n",
        encoding="utf-8",
    )
    (catalog_dir / "routes.py").write_text(
        "from hinoki import Access, BearerAuth, Caller, Includable, Schema, http\n\n"
        "class TokenAuth(BearerAuth[str]):\n"
        "    async def resolve_token(self, token: str) -> Caller[str]:\n"
        '        return Caller(id="u1", user=token, permissions={"catalog:read"})\n\n'
        'catalog_read = Access(auth=TokenAuth(), permissions=["catalog:read"])\n\n'
        "class ItemOut(Schema):\n"
        "    name: str\n\n"
        '@http.get("/items/featured", access=catalog_read)\n'
        "async def featured_item() -> ItemOut:\n"
        '    return ItemOut(name="widget")\n\n'
        "routes: list[Includable] = [featured_item]\n",
        encoding="utf-8",
    )
    (ledger_dir / "models.py").write_text(
        "from uuid import UUID, uuid4\n"
        "from hinoki.orm import Model, field\n\n"
        'class Entry(Model, table="ledger_entries"):\n'
        "    id: UUID = field(primary_key=True, default_factory=uuid4)\n"
        "    amount: int\n",
        encoding="utf-8",
    )
    (ledger_dir / "routes.py").write_text(
        "from hinoki import Includable, Schema, http\n\n"
        "class EntryOut(Schema):\n"
        "    amount: int\n\n"
        '@http.get("/entries")\n'
        "async def list_entries() -> list[EntryOut]:\n"
        "    return [EntryOut(amount=1)]\n\n"
        "routes: list[Includable] = [list_entries]\n",
        encoding="utf-8",
    )

    # Mount only catalog in shopproj/routes.py; ledger stays persistent without HTTP.
    (project / "shopproj" / "routes.py").write_text(
        "from hinoki import Includable, Mount\n"
        "from shopproj.api import health\n"
        "from shopproj.apps.catalog.routes import routes as catalog_routes\n\n"
        'routes: list[Includable] = [health, Mount("/catalog", catalog_routes)]\n',
        encoding="utf-8",
    )

    assert main(["migrations", "plan", "catalog"]) == 0
    assert main(["migrations", "plan", "ledger"]) == 0
    assert main(["migrations", "apply"]) == 0

    app = importlib.import_module("shopproj.main").app
    assert [route.path for route in app.routes] == [
        "/health",
        "/catalog/items/featured",
    ]
    assert _get(app, "/catalog/items/featured", bearer="ok") == (
        200,
        {"name": "widget"},
    )
    assert _get(app, "/ledger/entries", bearer="ok")[0] == 404


def test_api_only_app_and_transitive_persistence_dependency_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = create("mixedproj", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    _clear_package(monkeypatch, "mixedproj")

    assert main(["app", "create", "accounts", "--no-input"]) == 0
    assert (
        main(
            [
                "app",
                "create",
                "gateway",
                "--no-models",
                "--requires",
                "accounts",
                "--no-input",
            ]
        )
        == 0
    )
    assert main(["app", "create", "audit", "--requires", "gateway", "--no-input"]) == 0
    capsys.readouterr()

    accounts_dir = project / "mixedproj" / "apps" / "accounts"
    audit_dir = project / "mixedproj" / "apps" / "audit"
    gateway_dir = project / "mixedproj" / "apps" / "gateway"

    (accounts_dir / "models.py").write_text(
        "from uuid import UUID, uuid4\n"
        "from hinoki.orm import Model, field\n\n"
        'class Account(Model, table="accounts"):\n'
        "    id: UUID = field(primary_key=True, default_factory=uuid4)\n"
        "    name: str\n",
        encoding="utf-8",
    )
    (audit_dir / "models.py").write_text(
        "from uuid import UUID, uuid4\n"
        "from hinoki.orm import Model, field, foreign_key, OnDelete\n"
        "from mixedproj.apps.accounts.models import Account\n\n"
        'class AuditLog(Model, table="audit_logs"):\n'
        "    id: UUID = field(primary_key=True, default_factory=uuid4)\n"
        "    account_id: UUID = foreign_key(\n"
        '        Account, on_delete=OnDelete.RESTRICT, related_name="audits"\n'
        "    )\n",
        encoding="utf-8",
    )
    (gateway_dir / "routes.py").write_text(
        "from hinoki import Access, BearerAuth, Caller, Includable, Schema, http\n\n"
        "class TokenAuth(BearerAuth[str]):\n"
        "    async def resolve_token(self, token: str) -> Caller[str]:\n"
        '        return Caller(id="u1", user=token, permissions={"gateway:read"})\n\n'
        'gateway_read = Access(auth=TokenAuth(), permissions=["gateway:read"])\n\n'
        "class Pong(Schema):\n"
        "    pong: str\n\n"
        '@http.get("/ping", access=gateway_read)\n'
        "async def ping() -> Pong:\n"
        '    return Pong(pong="ok")\n\n'
        "routes: list[Includable] = [ping]\n",
        encoding="utf-8",
    )
    (project / "mixedproj" / "routes.py").write_text(
        "from hinoki import Includable, Mount\n"
        "from mixedproj.api import health\n"
        "from mixedproj.apps.gateway.routes import routes as gateway_routes\n\n"
        'routes: list[Includable] = [health, Mount("/gateway", gateway_routes)]\n',
        encoding="utf-8",
    )

    manifest = load_manifest()
    assert [app.name for app in manifest.ordered_apps()] == [
        "accounts",
        "gateway",
        "audit",
    ]
    assert [app.name for app in manifest.persistent_apps()] == ["accounts", "audit"]

    # Planning an API-only app explicitly fails clearly.
    assert main(["migrations", "plan", "gateway"]) == 1
    assert "has no models or migrations" in capsys.readouterr().err

    # Planning accounts then audit respects the transitive dependency through gateway.
    assert main(["migrations", "plan", "accounts"]) == 0
    assert main(["migrations", "plan", "audit"]) == 0
    assert main(["migrations", "apply"]) == 0
    assert main(["migrations", "status"]) == 0

    app = importlib.import_module("mixedproj.main").app
    assert _get(app, "/gateway/ping", bearer="ok") == (200, {"pong": "ok"})


def test_reusable_app_mounted_at_different_prefixes_in_two_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_root = tmp_path / "shared"
    (shared_root / "shared_billing").mkdir(parents=True)
    (shared_root / "shared_billing" / "__init__.py").write_text("", encoding="utf-8")
    (shared_root / "shared_billing" / "routes.py").write_text(
        "from hinoki import Includable, Schema, http\n\n"
        "class BillingStatus(Schema):\n"
        "    service: str\n\n"
        '@http.get("/status")\n'
        "async def billing_status() -> BillingStatus:\n"
        '    return BillingStatus(service="billing")\n\n'
        "routes: list[Includable] = [billing_status]\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(shared_root))
    for proj_name, mount_prefix in (("proj_a", "/billing"), ("proj_b", "/v2/payments")):
        proj = create(proj_name, tmp_path)
        (proj / proj_name / "routes.py").write_text(
            "from hinoki import Includable, Mount\n"
            f"from {proj_name}.api import health\n"
            "from shared_billing.routes import routes as billing_routes\n\n"
            "routes: list[Includable] = [\n"
            f'    health, Mount("{mount_prefix}", billing_routes)\n'
            "]\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(proj))
        _clear_package(monkeypatch, proj_name)
        app = importlib.import_module(f"{proj_name}.main").app
        assert [route.path for route in app.routes] == [
            "/health",
            f"{mount_prefix}/status",
        ]
        assert _get(app, f"{mount_prefix}/status") == (200, {"service": "billing"})


def test_route_conflict_in_project_routes_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("conflictproj", tmp_path)
    monkeypatch.syspath_prepend(str(project))
    _clear_package(monkeypatch, "conflictproj")
    (project / "conflictproj" / "routes.py").write_text(
        "from hinoki import Includable, Mount, http\n"
        "from conflictproj.api import health\n\n"
        '@http.get("/ping")\n'
        "def ping_a() -> dict[str, str]:\n"
        '    return {"a": "1"}\n\n'
        '@http.get("/ping")\n'
        "def ping_b() -> dict[str, str]:\n"
        '    return {"b": "2"}\n\n'
        "routes: list[Includable] = [\n"
        '    health, Mount("/dup", [ping_a]), Mount("/dup", [ping_b])\n'
        "]\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistrationError, match="GET /dup/ping"):
        importlib.import_module("conflictproj.main")


def test_partial_persistence_in_manifest_is_rejected(tmp_path: Path) -> None:
    for body in (
        'models = "a.models"',
        'migrations = "a.migrations"',
    ):
        manifest = tmp_path / "hinoki.toml"
        manifest.write_text(
            f'[project]\napplication = "a.main:app"\n\n[[apps]]\nname = "a"\n{body}\n',
            encoding="utf-8",
        )
        with pytest.raises(
            ProjectError,
            match="must declare both 'models' and 'migrations', or neither",
        ):
            load_manifest(manifest)


def test_migration_history_namespaces_stay_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = create("histproj", tmp_path, database="sqlite")
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    _clear_package(monkeypatch, "histproj")

    assert main(["app", "create", "billing", "--no-input"]) == 0
    (project / "histproj" / "apps" / "billing" / "models.py").write_text(
        "from uuid import UUID, uuid4\n"
        "from hinoki.orm import Model, field\n\n"
        'class Invoice(Model, table="invoices"):\n'
        "    id: UUID = field(primary_key=True, default_factory=uuid4)\n"
        "    total: int\n",
        encoding="utf-8",
    )
    assert main(["migrations", "plan", "billing"]) == 0
    db_path = project / ".hinoki" / "local.sqlite3"
    assert main(["migrations", "apply"]) == 0

    manifest = load_manifest()
    billing = manifest.app("billing")
    assert manifest.migration_namespace(billing) == "billing"

    async def check_status() -> list[tuple[str, str]]:
        database = await Database.connect(f"sqlite://{db_path}", max_size=1)
        try:
            return await migrator.status(
                database,
                manifest.migrations_path(billing),
                namespace=manifest.migration_namespace(billing),
            )
        finally:
            database.close()

    assert asyncio.run(check_status()) == [("0001-create-table-invoices", "applied")]


def test_examples_register_expected_rust_router_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    notes_root = repo_root / "examples" / "notes"
    monkeypatch.syspath_prepend(str(notes_root))
    _clear_package(monkeypatch, "notes")
    notes_app = importlib.import_module("notes.main").app
    assert sorted((route.method, route.path) for route in notes_app.routes) == [
        ("DELETE", "/notes/{item_id}"),
        ("GET", "/notes"),
        ("GET", "/notes/{item_id}"),
        ("PATCH", "/notes/{item_id}"),
        ("POST", "/notes"),
    ]

    tracker_root = repo_root / "examples" / "tracker"
    monkeypatch.syspath_prepend(str(tracker_root))
    _clear_package(monkeypatch, "tracker")
    tracker_app = importlib.import_module("tracker.main").app
    tracker_paths = sorted({route.path for route in tracker_app.routes})
    assert tracker_paths == [
        "/activity",
        "/activity/{item_id}",
        "/health",
        "/members/me",
        "/tasks",
        "/tasks/{item_id}",
        "/tasks/{item_id}/complete",
    ]
