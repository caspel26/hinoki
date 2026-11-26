"""Project-scoped commands for explicit modular-app registration."""

from __future__ import annotations

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

from hinoki import jsontext
from hinoki.cli.appprompts import ask_app, report_app, split_requires
from hinoki.cli.new import interactive
from hinoki.project import (
    ProjectApp,
    ProjectError,
    ProjectManifest,
    find_manifest,
    load_manifest,
)
from hinoki.scaffold import APP_API, APP_ROUTES, NAME, RESERVED_NAMES
from hinoki.terminal import Style

LOCK_NAME = ".hinoki-app-create.lock"


def create_app(
    name: str,
    start: Path | None = None,
    *,
    requires: tuple[str, ...] = (),
    no_models: bool = False,
) -> Path:
    """Create an app package and append it to the project manifest."""
    _validate_app_name(name)
    manifest_path = find_manifest(start)
    with _manifest_lock(manifest_path):
        return _create_app_locked(name, manifest_path, requires, no_models)


def _create_app_locked(
    name: str,
    manifest_path: Path,
    requires: tuple[str, ...],
    no_models: bool,
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
            name,
            module_name,
            app_directory,
            apps_directory,
            made_apps_init,
            no_models,
        )
        _append_app(
            manifest.path,
            manifest.path.read_bytes(),
            name,
            module_name,
            requires,
            no_models,
        )
    except OSError as error:
        _remove_app_package(
            app_directory, apps_directory, made_apps_directory, made_apps_init
        )
        raise ProjectError(f"could not create app {name!r}: {error}") from None
    return app_directory


@contextmanager
def _manifest_lock(manifest_path: Path) -> Iterator[None]:
    """Serialize app registration across processes on POSIX and Windows.

    The lock file is removed when the lock is released."""
    lock_path = manifest_path.parent / LOCK_NAME
    lock = _windows_lock if os.name == "nt" else _posix_lock
    with lock(lock_path):
        yield


@contextmanager
def _posix_lock(lock_path: Path) -> Iterator[None]:
    # POSIX flock is released by the kernel if the process exits.
    import fcntl  # noqa: PLC0415 - only available on POSIX

    while True:
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            lock_file.close()
            raise
        if _is_current_lock(lock_file, lock_path):
            break
        # The previous holder removed this file; lock the one at the path now.
        lock_file.close()
    with lock_file:
        try:
            yield
        finally:
            # Unlink before unlocking so a waiter on this file sees it is stale.
            with suppress(FileNotFoundError):
                lock_path.unlink()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _is_current_lock(lock_file: BinaryIO, lock_path: Path) -> bool:
    try:
        current = lock_path.stat()
    except FileNotFoundError:
        return False
    opened = os.fstat(lock_file.fileno())
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


@contextmanager
def _windows_lock(lock_path: Path) -> Iterator[None]:
    with lock_path.open("a+b") as lock_file:
        _lock_manifest_windows(lock_file)
        try:
            yield
        finally:
            _unlock_manifest_windows(lock_file)
    # Windows refuses to delete a file another process still has open, so a
    # waiting process keeps the file and locks it next.
    with suppress(OSError):
        lock_path.unlink()


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
        raise ProjectError(
            "app name must be lower-case letters and digits, with single underscores "
            "between words"
        )


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
    app_directory: Path,
    apps_directory: Path,
    made_apps_init: bool,
    no_models: bool,
) -> None:
    if made_apps_init:
        (apps_directory / "__init__.py").write_text(
            '"""Project app packages."""\n', encoding="utf-8"
        )
    (app_directory / "__init__.py").write_text(
        f'"""The {name} project app."""\n', encoding="utf-8"
    )
    (app_directory / "api.py").write_text(APP_API.format(name=name), encoding="utf-8")
    (app_directory / "routes.py").write_text(
        APP_ROUTES.format(name=name, package=package),
        encoding="utf-8",
    )
    if no_models:
        return
    (app_directory / "models.py").write_text(
        '"""Models owned by this app."""\n', encoding="utf-8"
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
    requires: tuple[str, ...],
    no_models: bool,
) -> None:
    module = f"{package}.apps.{name}"
    models = "" if no_models else f"{module}.models"
    migrations = "" if no_models else f"{module}.migrations"
    _append_block(
        path,
        original,
        _app_block(
            name,
            models=models,
            migrations=migrations,
            requires=requires,
        ),
    )


def _app_block(
    name: str,
    *,
    models: str = "",
    migrations: str = "",
    requires: tuple[str, ...] = (),
    events: str = "",
) -> str:
    lines = [
        "[[apps]]",
        f"name = {jsontext.toml_value(name)}",
    ]
    if models:
        lines.append(f"models = {jsontext.toml_value(models)}")
    if migrations:
        lines.append(f"migrations = {jsontext.toml_value(migrations)}")
    if events:
        lines.append(f"events = {jsontext.toml_value(events)}")
    lines.append(f"requires = {jsontext.toml_value(list(requires))}")
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
    models: str = "",
    migrations: str = "",
    requires: tuple[str, ...] = (),
    events: str = "",
    start: Path | None = None,
) -> Path:
    """Register an existing package as an app without creating any file."""
    _validate_app_name(name)
    if migrations and not models:
        raise ProjectError("pass --models when --migrations is set")
    manifest_path = find_manifest(start)
    with _manifest_lock(manifest_path):
        manifest = load_manifest(manifest_path)
        _check_duplicate_app(manifest, name)
        resolved_migrations = migrations or (
            _sibling_package(models, "migrations") if models else ""
        )
        block = _app_block(
            name,
            models=models,
            migrations=resolved_migrations,
            requires=requires,
            events=events,
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
    create.add_argument(
        "--no-models",
        action="store_true",
        help="create an API-only app without models.py or migrations/",
    )
    create.add_argument(
        "--requires", default=None, help="comma-separated apps this one depends on"
    )
    create.add_argument(
        "--no-input", action="store_true", help="never prompt; use the defaults"
    )
    add = actions.add_parser("add", help="register an existing package as an app")
    add.add_argument("name")
    add.add_argument(
        "--models", default="", help="dotted models module (omit for API-only apps)"
    )
    add.add_argument(
        "--migrations",
        default="",
        help="dotted migrations package (default: next to the models module)",
    )
    add.add_argument(
        "--events", default="", help="module:attribute of the app's Handlers"
    )
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
                "requires": list(app.requires),
                "events": app.events,
            }
            for app in apps
        ]
        print(jsontext.dumps({"apps": rows}, indent=2))
        return 0
    if not apps:
        print("no apps registered")
        return 0
    for app in apps:
        persistence = (
            f"  models: {app.models}\n  migrations: {app.migrations}\n"
            if app.has_persistence
            else "  models: none\n"
        )
        print(
            f"{app.name}\n"
            f"{persistence}"
            f"{_events_line(app)}"
            f"  requires: {', '.join(app.requires) or '—'}"
        )
    return 0


def _events_line(app: ProjectApp) -> str:
    return f"  events: {app.events}\n" if app.events else ""


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
    no_models = bool(getattr(args, "no_models", False))
    created = create_app(args.name, requires=requires, no_models=no_models)
    manifest_path = find_manifest()
    for line in report_app(
        style,
        args.name,
        created,
        manifest_path,
        requires,
        no_models=no_models,
    ):
        print(line)
    return 0


def add_app(args: Namespace) -> int:
    """Register an existing package; the error handling lives in ``run``."""
    path = register_app(
        args.name,
        models=args.models,
        migrations=args.migrations,
        events=args.events,
        requires=tuple(item for item in args.requires.split(",") if item),
    )
    print(f"registered app {args.name} in {path}")
    return 0
