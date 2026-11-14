"""Load ``module:object`` targets from the current project checkout."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def add_project_to_import_path() -> None:
    """Make the caller's project importable from installed console scripts."""
    project = str(Path.cwd())
    if "" not in sys.path and project not in sys.path:
        sys.path.insert(0, project)


def load_target(spec: str) -> object:
    """Import a project's target, including dotted attributes.

    Console scripts put their executable directory on ``sys.path``. Add the
    caller's project directory so an app in the current checkout is importable
    just as it is with ``python -m``.
    """
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"expected module:attribute, got {spec!r}")
    add_project_to_import_path()
    found: object = importlib.import_module(module_name)
    for part in attribute.split("."):
        found = getattr(found, part)
    return found
