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
    spec = importlib.util.find_spec(package)  # a top-level name imports nothing
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


def literal(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        return value if isinstance(value, str) else None
    return None


def dynamic_target(call: ast.Call) -> tuple[bool, str] | None:
    """``(True, name)`` for a dynamic import call, ``None`` for any other call.
    ``name`` is empty when the module name is not a string literal."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in DYNAMIC_CALLS:
        return None
    return True, literal(call) or ""


def imports_of(module: str, path: Path) -> Iterator[Import]:
    """Every import in a file, including those inside functions and
    ``if TYPE_CHECKING`` blocks (they are still architectural dependencies)."""
    shown = os.path.relpath(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
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
            found = dynamic_target(node)
            if found is not None:
                yield Import(module, found[1], shown, node.lineno)


def within(name: str, prefix: str) -> bool:
    """``name`` is ``prefix`` or inside it."""
    return name == prefix or name.startswith(prefix + ".")
