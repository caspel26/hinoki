"""The ORM lives under ``hinoki.orm``; old import paths still resolve."""

import ast
import importlib
from pathlib import Path

import hinoki.orm
from hinoki.orm import Model, QuerySet, Related, field, foreign_key, use_database
from hinoki.orm.migrations import plan
from hinoki.orm.migrator import apply
from hinoki.orm.relations import Relation

FORBIDDEN = (
    "hinoki.routing",
    "hinoki.runtime",
    "hinoki.app",
    "hinoki.admin",
    "hinoki.mcp",
)


def test_public_names_come_from_the_package() -> None:
    assert all(
        callable(x) or isinstance(x, type)
        for x in (Model, QuerySet, Related, Relation, field, foreign_key, use_database)
    )
    assert callable(plan) and callable(apply)
    assert hinoki.orm.Model is importlib.import_module("hinoki.orm.model").Model


def test_old_import_paths_alias_the_new_modules() -> None:
    for old, new in (
        ("hinoki.relations", "hinoki.orm.relations"),
        ("hinoki.migrations", "hinoki.orm.migrations"),
        ("hinoki.migrator", "hinoki.orm.migrator"),
    ):
        assert importlib.import_module(old) is importlib.import_module(new)


def test_the_orm_does_not_depend_on_the_web_layers() -> None:
    root = Path(hinoki.orm.__file__).parent
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                assert not name.startswith(FORBIDDEN), f"{path.name} imports {name}"
