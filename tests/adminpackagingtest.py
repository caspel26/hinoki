"""Tests for ``hinoki.admin`` packaging, assets, migrations and explicit activation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import hinoki.admin.ui
import hinoki.admin.users.migrations
from hinoki import Hinoki, Mount
from hinoki.admin import Admin


def test_admin_ui_assets_are_packaged() -> None:
    ui_dir = Path(next(iter(hinoki.admin.ui.__path__)))
    for filename in (
        "admin.js",
        "base.css",
        "icons.svg",
        "LICENSE-lucide",
        "THIRD-PARTY.md",
    ):
        assert (ui_dir / filename).is_file(), f"missing admin asset {filename}"
    components = ui_dir / "components"
    assert components.is_dir()
    assert any(components.glob("*.html"))


def test_admin_user_migrations_are_packaged() -> None:
    migrations_dir = Path(next(iter(hinoki.admin.users.migrations.__path__)))
    for filename in (
        "__init__.py",
        "0001-initial.sql",
        "0001-initial.state.json",
        "0002-login-windows.sql",
        "0002-login-windows.state.json",
        "0003-rate-limits.sql",
        "0003-rate-limits.state.json",
    ):
        assert (migrations_dir / filename).is_file(), (
            f"missing admin migration {filename}"
        )


def test_admin_requires_explicit_mount_and_no_admin_extra_exists() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    extras = pyproject["project"].get("optional-dependencies", {})
    assert "admin" not in extras

    plain = Hinoki()
    assert not plain.routes

    site = Admin(site="Test Admin", secret="s" * 48)
    mounted = Hinoki(include=[Mount("/admin", site)])
    assert any(route.path.startswith("/admin/") for route in mounted.routes)
