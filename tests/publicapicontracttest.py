"""Public API surface, reference directive and example import contract checks."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import hinoki
from hinoki import rules

PACKAGES = (
    "hinoki",
    "hinoki.http",
    "hinoki.db",
    "hinoki.orm",
    "hinoki.admin",
    "hinoki.mail",
    "hinoki.cache",
    "hinoki.sessions",
    "hinoki.storage",
)
REGISTRATION_PATHS = (
    "hinoki.admin.users.models",
    "hinoki.admin.users.migrations",
    "hinoki.ratelimit_store.models",
    "hinoki.ratelimit_store.migrations",
    "hinoki.cache.migrations_path",
)


def _resolve_target(target: str) -> object | None:
    parts = target.split(".")
    for split in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split])
        try:
            obj: object = importlib.import_module(module_name)
            for attr in parts[split:]:
                obj = getattr(obj, attr)
        except (ImportError, AttributeError):
            continue
        return obj
    return None


def test_core_packages_export_all_declared_symbols_without_legacy_leaks() -> None:
    for package_name in PACKAGES:
        module = importlib.import_module(package_name)
        exports = getattr(module, "__all__", ())
        assert exports, f"{package_name} has an empty __all__"
        assert "Reply" not in exports
        assert not hasattr(module, "Reply")
        for symbol in exports:
            assert hasattr(module, symbol), f"{package_name}.{symbol} is missing"


def test_rule_families_are_available_only_from_the_rules_namespace() -> None:
    families = {"Actions", "Imports", "Models", "Schemas", "Transactions"}
    assert "rules" in hinoki.__all__
    assert not families.intersection(hinoki.__all__)
    assert all(hasattr(rules, name) for name in families)
    assert all(not hasattr(hinoki, name) for name in families)


def test_every_reference_directive_resolves_to_a_real_object() -> None:
    ref_dir = Path(__file__).resolve().parents[1] / "docs" / "reference"
    checked = 0
    for markdown in sorted(ref_dir.glob("*.md")):
        for line in markdown.read_text(encoding="utf-8").splitlines():
            if not line.startswith("::: "):
                continue
            target = line[4:].strip()
            checked += 1
            assert _resolve_target(target) is not None, (
                f"{markdown.name}: broken reference target {target!r}"
            )
    assert checked > 0


def _hinoki_imports(source: Path) -> list[tuple[str, str]]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("hinoki")
        ):
            found.extend(
                (node.module, alias.name) for alias in node.names if alias.name != "*"
            )
    return found


def test_every_example_hinoki_import_resolves() -> None:
    examples_dir = Path(__file__).resolve().parents[1] / "examples"
    imports = [
        item
        for source in sorted(examples_dir.rglob("*.py"))
        for item in _hinoki_imports(source)
    ]
    assert imports
    for module_name, symbol in imports:
        module = importlib.import_module(module_name)
        if not hasattr(module, symbol):
            importlib.import_module(f"{module_name}.{symbol}")


def test_promised_registration_paths_resolve() -> None:
    for target in REGISTRATION_PATHS:
        assert _resolve_target(target) is not None, (
            f"promised registration path {target!r} did not resolve"
        )
