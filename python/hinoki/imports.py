"""Static import scanning for the ``Imports`` rules.

Reads source files with ``ast``; nothing is imported or executed. Limits, stated
in every diagnostic that depends on them: only the project's own packages are
scanned, imports reached through third-party code are not followed, and a
dynamic import whose module name is not a literal is reported as unverifiable.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DYNAMIC_CALLS = {"import_module", "__import__"}


@dataclass(frozen=True, slots=True)
class Import:
    """One import statement. ``target`` is empty for an unresolvable dynamic
    import."""

    module: str
    target: str
    file: str
    line: int


def package_modules(package: str) -> Iterator[tuple[str, Path]]:
    """``(module name, file)`` for every ``.py`` file of a top-level package."""
    spec = importlib.util.find_spec(package)  # a dotted name imports its parents
    if spec is None or not spec.submodule_search_locations:
        raise ValueError(f"cannot find package {package!r}")
    for location in spec.submodule_search_locations:
        root = Path(location)
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            yield ".".join((package, *parts)), path


def absolute(module: str, is_package: bool, level: int, name: str | None) -> str:
    """Resolve a relative import to an absolute dotted name."""
    if level == 0:
        return name or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, *([name] if name else [])])


def text_of(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def dynamic_targets(
    call: ast.Call, module: str, is_package: bool, names: set[str]
) -> list[str] | None:
    """Modules a dynamic import call names, ``[""]`` when it cannot be told, and
    ``None`` when the call is not an import. ``names`` holds the local aliases of
    ``import_module`` / ``__import__`` found in the file."""
    name = call_name(call)
    if name not in DYNAMIC_CALLS and name not in names:
        return None
    wanted = text_of(argument(call, 0, "name"))
    if wanted is None:
        return [""]
    if wanted.startswith("."):
        anchor = argument(call, 1, "package")
        package = text_of(anchor)
        if (
            package is None
            and isinstance(anchor, ast.Name)
            and anchor.id == "__package__"
        ):
            package = module if is_package else module.rpartition(".")[0]
        if package is None:
            return [""]
        wanted = absolute(
            package, True, len(wanted) - len(wanted.lstrip(".")), wanted.lstrip(".")
        )
    found = [wanted]
    listed = argument(call, 3, "fromlist") if name == "__import__" else None
    if isinstance(listed, ast.List | ast.Tuple):
        found += [f"{wanted}.{text_of(i)}" for i in listed.elts if text_of(i)]
    return found


def aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``import_module`` / ``__import__``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                a.asname for a in node.names if a.asname and a.name in DYNAMIC_CALLS
            )
    return names


def imports_of(module: str, path: Path) -> Iterator[Import]:
    """Every import in a file, including those inside functions and
    ``if TYPE_CHECKING`` blocks (they are still architectural dependencies)."""
    shown = os.path.relpath(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    local = aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Import(module, alias.name, shown, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = absolute(module, is_package, node.level, node.module)
            yield Import(module, base, shown, node.lineno)
            for alias in node.names:  # `from pkg import sub` may import pkg.sub
                yield Import(module, f"{base}.{alias.name}", shown, node.lineno)
        elif isinstance(node, ast.Call):
            for target in dynamic_targets(node, module, is_package, local) or []:
                yield Import(module, target, shown, node.lineno)


def within(name: str, prefix: str) -> bool:
    """``name`` is ``prefix`` or inside it."""
    return name == prefix or name.startswith(prefix + ".")
