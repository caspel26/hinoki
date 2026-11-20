"""Project-scoped commands for explicit modular-app registration."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from argparse import ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from hinoki.appprompts import ask_app, report_app, split_requires
from hinoki.newcli import interactive
from hinoki.project import (
    ProjectApp,
    ProjectError,
    ProjectManifest,
    find_manifest,
    load_manifest,
)
from hinoki.scaffold import APP_API, APP_ROUTES, NAME, RESERVED_NAMES
from hinoki.terminal import Style


def create_app(
    name: str,
    start: Path | None = None,
    *,
    prefix: str = "",
    requires: tuple[str, ...] = (),
) -> Path:
    """Create a model/migration package and append it to the project manifest."""
    _validate_app_name(name)
    manifest_path = find_manifest(start)
    with _manifest_lock(manifest_path):
        return _create_app_locked(name, manifest_path, prefix or f"/{name}", requires)


def _create_app_locked(
    name: str, manifest_path: Path, prefix: str, requires: tuple[str, ...]
) -> Path:
    """Re-read and update the manifest while holding its process lock."""
    manifest = load_manifest(manifest_path)
    _check_duplicate_app(manifest, name)
    module_name, apps_directory, app_directory = _app_paths(manifest, name)
    made_apps_directory, made_apps_init = _create_app_package(
        name, apps_directory, app_directory
    )
    try:
        _write_app_files(
            name, module_name, prefix, app_directory, apps_directory, made_apps_init
        )
        _append_app(
            manifest.path,
            manifest.path.read_bytes(),
            name,
            module_name,
            prefix,
            requires,
        )
    except OSError as error:
        _remove_app_package(
            app_directory, apps_directory, made_apps_directory, made_apps_init
        )
        raise ProjectError(f"could not create app {name!r}: {error}") from None
    return app_directory


@contextmanager
def _manifest_lock(manifest_path: Path) -> Iterator[None]:
    """Serialize app registration across processes on POSIX and Windows."""
    lock_path = manifest_path.parent / ".hinoki-app-create.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            _lock_manifest_windows(lock_file)
        else:
            # POSIX flock is released by the kernel if the process exits.
            import fcntl  # noqa: PLC0415 - only available on POSIX

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                _unlock_manifest_windows(lock_file)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _lock_manifest_windows(lock_file: BinaryIO) -> None:
    """Lock a byte range using the Windows standard library."""
    import msvcrt  # noqa: PLC0415 - only available on Windows

    lock_file.seek(0)
    if not lock_file.read(1):
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]


def _unlock_manifest_windows(lock_file: BinaryIO) -> None:
    """Release the manifest lock's byte range on Windows."""
    import msvcrt  # noqa: PLC0415 - only available on Windows

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


def _validate_app_name(name: str) -> None:
    if not NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise ProjectError("app name must be lower-case letters and digits")


def _check_duplicate_app(manifest: ProjectManifest, name: str) -> None:
    if any(app.name == name for app in manifest.apps):
        raise ProjectError(f"app {name!r} is already registered")


def _app_paths(manifest: ProjectManifest, name: str) -> tuple[str, Path, Path]:
    module_name = manifest.application.partition(":")[0].split(".", maxsplit=1)[0]
    package = manifest.root / module_name
    if not (package / "__init__.py").is_file():
        raise ProjectError(
            f"project package {module_name!r} needs an __init__.py before adding apps"
        )
    apps_directory = package / "apps"
    app_directory = apps_directory / name
    if app_directory.exists():
        raise ProjectError(f"{app_directory} already exists")
    return module_name, apps_directory, app_directory


def _create_app_package(
    name: str, apps_directory: Path, app_directory: Path
) -> tuple[bool, bool]:
    made_apps_directory = not apps_directory.exists()
    apps_directory.mkdir(exist_ok=True)
    try:
        app_directory.mkdir()
    except OSError as error:
        if made_apps_directory:
            with suppress(OSError):
                apps_directory.rmdir()
        raise ProjectError(f"could not create app {name!r}: {error}") from None
    return made_apps_directory, not (apps_directory / "__init__.py").exists()


def _write_app_files(
    name: str,
    package: str,
    prefix: str,
    app_directory: Path,
    apps_directory: Path,
    made_apps_init: bool,
) -> None:
    if made_apps_init:
        (apps_directory / "__init__.py").write_text(
            '"""Project app packages."""\n', encoding="utf-8"
        )
    (app_directory / "__init__.py").write_text(
        f'"""The {name} project app."""\n', encoding="utf-8"
    )
    (app_directory / "models.py").write_text(
        '"""Models owned by this app."""\n', encoding="utf-8"
    )
    (app_directory / "api.py").write_text(APP_API.format(name=name), encoding="utf-8")
    (app_directory / "routes.py").write_text(
        APP_ROUTES.format(name=name, package=package, prefix=prefix),
        encoding="utf-8",
    )
    migrations_directory = app_directory / "migrations"
    migrations_directory.mkdir()
    (migrations_directory / "__init__.py").write_text("", encoding="utf-8")


def _remove_app_package(
    app_directory: Path,
    apps_directory: Path,
    made_apps_directory: bool,
    made_apps_init: bool,
) -> None:
    shutil.rmtree(app_directory, ignore_errors=True)
    if made_apps_init:
        (apps_directory / "__init__.py").unlink(missing_ok=True)
    if made_apps_directory:
        with suppress(OSError):
            apps_directory.rmdir()


def _append_app(
    path: Path,
    original: bytes,
    name: str,
    package: str,
    prefix: str,
    requires: tuple[str, ...],
) -> None:
    module = f"{package}.apps.{name}"
    _append_block(
        path,
        original,
        _app_block(
            name,
            models=f"{module}.models",
            migrations=f"{module}.migrations",
            routes=f"{module}.routes:routes",
            prefix=prefix,
            requires=requires,
        ),
    )


def _app_block(
    name: str,
    *,
    models: str,
    migrations: str,
    routes: str,
    prefix: str,
    requires: tuple[str, ...],
) -> str:
    lines = [
        "[[apps]]",
        f"name = {json.dumps(name)}",
        f"models = {json.dumps(models)}",
        f"migrations = {json.dumps(migrations)}",
    ]
    if routes:
        lines.append(f"routes = {json.dumps(routes)}")
    if prefix:
        lines.append(f"prefix = {json.dumps(prefix)}")
    lines.append(f"requires = {json.dumps(list(requires))}")
    return "\n".join(lines) + "\n"


def _append_block(path: Path, original: bytes, block: str) -> None:
    """Append an ``[[apps]]`` block, validating the whole manifest before it
    replaces the file."""
    text = original.decode("utf-8")
    separator = "" if not text or text.endswith("\n\n") else "\n"
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hinoki-", suffix=".toml", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(original + (separator + block).encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        load_manifest(Path(temporary_name))
        os.replace(temporary_name, path)
    except (OSError, ProjectError):
        Path(temporary_name).unlink(missing_ok=True)
        raise


def register_app(
    name: str,
    *,
    models: str,
    migrations: str = "",
    routes: str = "",
    prefix: str = "",
    requires: tuple[str, ...] = (),
    start: Path | None = None,
) -> Path:
    """Register an existing package as an app without creating any file."""
    _validate_app_name(name)
    manifest_path = find_manifest(start)
    with _manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        _check_duplicate_app(manifest, name)
        block = _app_block(
            name,
            models=models,
            migrations=migrations or _sibling_package(models, "migrations"),
            routes=routes,
            prefix=prefix,
            requires=requires,
        )
        try:
            _append_block(manifest.path, manifest.path.read_bytes(), block)
        except OSError as error:
            raise ProjectError(f"could not register app {name!r}: {error}") from None
    return manifest.path


def _sibling_package(models: str, leaf: str) -> str:
    """``pkg.apps.x.models`` -> ``pkg.apps.x.<leaf>``; a bare module has no parent."""
    parent, dot, _ = models.rpartition(".")
    if not dot:
        raise ProjectError("pass --migrations: the models module has no parent package")
    return f"{parent}.{leaf}"


def add_arguments(commands: _SubParsersAction[ArgumentParser]) -> None:
    """Register modular-app project commands."""
    group = commands.add_parser("app", help="manage project apps")
    actions = group.add_subparsers(dest="app_action", required=True)
    listing = actions.add_parser("list", help="list registered project apps")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    create = actions.add_parser("create", help="create a modular app")
    create.add_argument("name", nargs="?", help="app name (asked for in a terminal)")
    create.add_argument("--prefix", default=None, help="mount prefix (default: /NAME)")
    create.add_argument(
        "--requires", default=None, help="comma-separated apps this one depends on"
    )
    create.add_argument(
        "--no-input", action="store_true", help="never prompt; use the defaults"
    )
    add = actions.add_parser("add", help="register an existing package as an app")
    add.add_argument("name")
    add.add_argument("--models", required=True, help="dotted models module")
    add.add_argument(
        "--migrations",
        default="",
        help="dotted migrations package (default: next to the models module)",
    )
    add.add_argument("--routes", default="", help="module:attribute of the routes")
    add.add_argument("--prefix", default="", help="mount prefix (default: /NAME)")
    add.add_argument(
        "--requires", default="", help="comma-separated apps this one depends on"
    )


def list_apps(args: Namespace) -> int:
    """List manifest app registrations without importing app modules."""
    try:
        manifest = load_manifest()
        apps = manifest.ordered_apps()
    except (ProjectError, OSError) as error:
        message = str(error)
        if message.startswith("no hinoki.toml found"):
            message = (
                "no hinoki.toml found; run this command from the project directory"
            )
        print(f"error: {message}", file=sys.stderr)
        return 2
    if args.format == "json":
        rows = [
            {
                "name": app.name,
                "models": app.models,
                "migrations": app.migrations,
                "routes": app.routes,
                "prefix": app.prefix or (f"/{app.name}" if app.routes else ""),
                "requires": list(app.requires),
            }
            for app in apps
        ]
        print(json.dumps({"apps": rows}, indent=2))
        return 0
    if not apps:
        print("no apps registered")
        return 0
    for app in apps:
        print(
            f"{app.name}\n"
            f"  models: {app.models}\n"
            f"  migrations: {app.migrations}\n"
            f"{_routes_line(app)}"
            f"  requires: {', '.join(app.requires) or '—'}"
        )
    return 0


def _routes_line(app: ProjectApp) -> str:
    if not app.routes:
        return ""
    return f"  routes: {app.routes} at {app.prefix or f'/{app.name}'}\n"


def run(args: Namespace) -> int:
    """Run one app-management command with user-friendly errors."""
    if args.app_action == "list":
        return list_apps(args)
    try:
        if args.app_action == "add":
            return add_app(args)
        return create_command(args)
    except (ProjectError, OSError) as error:
        print(Style.detect(sys.stderr).error(str(error)), file=sys.stderr)
        return 2


def create_command(args: Namespace) -> int:
    """Ask for what the flags left unset in a terminal, then create the app."""
    style = Style.detect()
    if interactive(args):
        code = ask_app(args, load_manifest(), input, style)
        if code:
            return code
    if args.name is None:
        message = "an app name is required (or run in a terminal)"
        print(style.error(message), file=sys.stderr)
        return 2
    requires = split_requires(args.requires or "")
    created = create_app(args.name, prefix=args.prefix or "", requires=requires)
    manifest = find_manifest()
    for line in report_app(
        style, args.name, created, manifest, args.prefix or "", requires
    ):
        print(line)
    return 0


def add_app(args: Namespace) -> int:
    """Register an existing package; the error handling lives in ``run``."""
    path = register_app(
        args.name,
        models=args.models,
        migrations=args.migrations,
        routes=args.routes,
        prefix=args.prefix,
        requires=tuple(item for item in args.requires.split(",") if item),
    )
    print(f"registered app {args.name} in {path}")
    return 0
