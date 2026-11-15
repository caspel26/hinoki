"""Project-aware interactive Python shell command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from hinoki.app import Hinoki
from hinoki.checkcli import USAGE_ERRORS, build_app
from hinoki.errors import ShutdownError, StartupError
from hinoki.orm import Model
from hinoki.orm.migrations import models_of
from hinoki.project import ProjectError, find_manifest_or_none, load_manifest
from hinoki.shell import ShellRuntime, run_console, terminal_reader


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the ``shell`` command and its explicit model override."""
    shell = commands.add_parser("shell", help="open an interactive app shell")
    shell.add_argument("target", nargs="?", help="module:app (default: project app)")
    shell.add_argument(
        "--models", action="append", help="model module; repeat to select more"
    )


def model_namespace(modules: Sequence[str]) -> dict[str, type[Model]]:
    """Load concrete models and reject ambiguous names before opening resources."""
    found: dict[str, type[Model]] = {}
    for module_name in modules:
        try:
            models = models_of(module_name)
        except Exception as error:
            raise ValueError(
                f"cannot load model module {module_name!r}: {error}"
            ) from None
        for model in models:
            previous = found.get(model.__name__)
            if previous is not None:
                raise ValueError(
                    f"model name {model.__name__!r} occurs in both "
                    f"{previous.__module__!r} and {module_name!r}"
                )
            if model.__name__ in {"app", "settings"}:
                raise ValueError(f"model name {model.__name__!r} is reserved")
            found[model.__name__] = model
    return found


def _target_and_models(
    target: str | None, selected_models: Sequence[str] | None
) -> tuple[str, tuple[str, ...]]:
    """Resolve the manifest default and registered model modules."""
    manifest_path = find_manifest_or_none()
    if manifest_path is None:
        if target is None:
            raise ProjectError("pass TARGET outside a project with hinoki.toml")
        return target, tuple(selected_models or ())

    manifest = load_manifest(manifest_path)
    if str(manifest.root) not in sys.path:
        sys.path.insert(0, str(manifest.root))
    modules = (
        tuple(selected_models)
        if selected_models
        else tuple(item.models for item in manifest.ordered_apps())
    )
    return target or manifest.application, modules


def run(
    args: argparse.Namespace,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Resolve an app, start its modules, and own the complete shell session."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if not stdin.isatty():
        print("error: `hinoki shell` requires an interactive terminal", file=stderr)
        return 2
    try:
        target, model_modules = _target_and_models(args.target, args.models)
        models = model_namespace(model_modules)
        app = build_app(target)
    except (*USAGE_ERRORS, ProjectError, OSError) as error:
        print(f"error: {error}", file=stderr)
        return 2

    return _run_session(app, models, stdin, stdout, stderr)


def _run_session(
    app: Hinoki,
    models: dict[str, type[Model]],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Own one runtime and report startup, prompt and shutdown failures."""
    namespace: dict[str, object] = {
        "__name__": "__hinoki_shell__",
        "app": app,
        "settings": app.settings,
        **models,
    }
    runtime = ShellRuntime(app)
    status = 0
    try:
        runtime.start(namespace)
        print("Hinoki interactive shell; use Ctrl-D or Ctrl-C to exit.", file=stdout)
        run_console(runtime, read=terminal_reader(stdin, stdout), output=stdout)
    except (StartupError, ShutdownError) as error:
        print(f"error: {error}", file=stderr)
        status = 1
    except KeyboardInterrupt:
        print("", file=stdout)
    except BaseException as error:
        print(f"error: {error}", file=stderr)
        status = 1
    finally:
        if runtime.thread.is_alive():
            try:
                runtime.close()
            except ShutdownError as error:
                print(f"error during shell shutdown: {error}", file=stderr)
                status = 1
    return status
