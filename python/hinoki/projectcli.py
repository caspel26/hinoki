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

from hinoki.project import ProjectError, ProjectManifest, find_manifest, load_manifest
from hinoki.scaffold import NAME, RESERVED_NAMES


def create_app(name: str, start: Path | None = None) -> Path:
    """Create a model/migration package and append it to the project manifest."""
    _validate_app_name(name)
    manifest_path = find_manifest(start)
    with _manifest_lock(manifest_path):
        return _create_app_locked(name, manifest_path)


def _create_app_locked(name: str, manifest_path: Path) -> Path:
    """Re-read and update the manifest while holding its process lock."""
    manifest = load_manifest(manifest_path)
    _check_duplicate_app(manifest, name)
    module_name, apps_directory, app_directory = _app_paths(manifest, name)
    made_apps_directory, made_apps_init = _create_app_package(
        name, apps_directory, app_directory
    )
    try:
        _write_app_files(name, app_directory, apps_directory, made_apps_init)
        _append_app(manifest.path, manifest.path.read_bytes(), name, module_name)
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
    name: str, app_directory: Path, apps_directory: Path, made_apps_init: bool
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


def _append_app(path: Path, original: bytes, name: str, package: str) -> None:
    text = original.decode("utf-8")
    separator = "" if not text or text.endswith("\n\n") else "\n"
    module = f"{package}.apps.{name}"
    block = (
        f"{separator}[[apps]]\n"
        f'name = "{name}"\n'
        f'models = "{module}.models"\n'
        f'migrations = "{module}.migrations"\n'
        "requires = []\n"
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hinoki-", suffix=".toml", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(original + block.encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        os.replace(temporary_name, path)
    except OSError:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def add_arguments(commands: _SubParsersAction[ArgumentParser]) -> None:
    """Register the `app create` parser on argparse's subparsers object."""
    group = commands.add_parser("app", help="create and inspect project apps")
    actions = group.add_subparsers(dest="app_action", required=True)
    create = actions.add_parser("create", help="create a modular app")
    create.add_argument("name")


def run(args: Namespace) -> int:
    """Run one app-management command with user-friendly errors."""
    try:
        created = create_app(args.name)
    except (ProjectError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"created app {args.name} at {created}")
    return 0
