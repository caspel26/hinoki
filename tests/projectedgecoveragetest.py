"""Project CLI edge cases that are easy to miss in the integration workflow."""

from __future__ import annotations

import os
import sys
import threading
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hinoki import projectcli
from hinoki.project import ProjectApp, ProjectError, ProjectManifest


def _manifest(root: Path, *, apps: tuple[ProjectApp, ...] = ()) -> ProjectManifest:
    path = root / "hinoki.toml"
    path.write_text('[project]\napplication = "sample.main:app"\n', encoding="utf-8")
    package = root / "sample"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").touch()
    return ProjectManifest(path, "sample.main:app", apps)


@pytest.mark.parametrize(
    "name", ["", "Bad", "bad-name", "bad__name", "bad_", "_bad", "class", "hinoki"]
)
def test_create_app_rejects_invalid_or_reserved_names(
    name: str, tmp_path: Path
) -> None:
    with pytest.raises(ProjectError, match="app name must be lower-case"):
        projectcli.create_app(name, tmp_path)


def test_create_app_rejects_duplicate_and_missing_project_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        tmp_path,
        apps=(
            ProjectApp("billing", "sample.billing", "sample.billing_migrations", ()),
        ),
    )
    monkeypatch.setattr(projectcli, "find_manifest", lambda _start: manifest.path)
    monkeypatch.setattr(projectcli, "load_manifest", lambda _path: manifest)
    with pytest.raises(ProjectError, match="already registered"):
        projectcli.create_app("billing")

    (tmp_path / "sample/__init__.py").unlink()
    with pytest.raises(ProjectError, match=r"needs an __init__\.py"):
        projectcli.create_app("accounts")


def test_create_app_rejects_existing_app_directory(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    apps = tmp_path / "sample/apps"
    (apps / "billing").mkdir(parents=True)
    with pytest.raises(ProjectError, match="already exists"):
        projectcli._app_paths(manifest, "billing")


def test_create_app_package_removes_new_apps_directory_when_mkdir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = tmp_path / "sample/apps"
    app = apps / "billing"
    apps.parent.mkdir()
    original = Path.mkdir

    def fail_app_directory(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path == app:
            raise OSError("injected mkdir failure")
        original(path, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_app_directory)
    with pytest.raises(ProjectError, match="injected mkdir failure"):
        projectcli._create_app_package("billing", apps, app)
    assert not apps.exists()


def test_create_app_keeps_existing_apps_init_and_rolls_back_on_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    apps = tmp_path / "sample/apps"
    apps.mkdir()
    init = apps / "__init__.py"
    init.write_text("existing\n", encoding="utf-8")
    monkeypatch.setattr(projectcli, "find_manifest", lambda _start: manifest.path)

    def fail_replace(*_args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("hinoki.projectcli.os.replace", fail_replace)
    with pytest.raises(ProjectError, match="could not create app 'billing'"):
        projectcli.create_app("billing")
    assert init.read_text(encoding="utf-8") == "existing\n"
    assert not (apps / "billing").exists()
    assert not list(tmp_path.glob(".hinoki-*.toml"))


def test_windows_manifest_lock_initializes_byte_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int]] = []
    fake_msvcrt = SimpleNamespace(
        LK_LOCK=1,
        LK_UNLCK=2,
        locking=lambda descriptor, mode, count: calls.append((descriptor, mode, count)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setitem(projectcli.__dict__, "os", SimpleNamespace(name="nt"))
    manifest = tmp_path / "hinoki.toml"
    manifest.touch()

    with projectcli._manifest_lock(manifest):
        assert (tmp_path / ".hinoki-app-create.lock").read_bytes() == b"0"
    assert [mode for _, mode, _ in calls] == [fake_msvcrt.LK_LOCK, fake_msvcrt.LK_UNLCK]


def test_posix_lock_waiter_retries_when_the_holder_removes_the_file(
    tmp_path: Path,
) -> None:
    fcntl = pytest.importorskip("fcntl")
    manifest = tmp_path / "hinoki.toml"
    manifest.touch()
    lock_path = tmp_path / projectcli.LOCK_NAME
    holder = lock_path.open("a+b")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    acquired = threading.Event()

    def wait_for_lock() -> bool:
        with projectcli._manifest_lock(manifest):
            acquired.set()
            # holding the lock on the file at the path, not on the removed one
            return lock_path.exists()

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiter = executor.submit(wait_for_lock)
        assert not acquired.wait(0.2)
        lock_path.unlink()
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        assert waiter.result(timeout=5)
    assert not lock_path.exists()


def test_app_management_run_reports_project_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        projectcli,
        "create_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProjectError("invalid project")
        ),
    )
    args = Namespace(
        app_action="create",
        name="billing",
        prefix=None,
        requires=None,
        events=False,
        no_input=True,
    )
    assert projectcli.run(args) == 2
    assert capsys.readouterr().err == "error: invalid project\n"


def test_posix_lock_closes_the_file_when_waiting_for_it_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fcntl = pytest.importorskip("fcntl")
    manifest = tmp_path / "hinoki.toml"
    manifest.touch()
    descriptors: list[int] = []

    def interrupted(descriptor: int, _operation: int) -> None:
        descriptors.append(descriptor)
        raise KeyboardInterrupt

    monkeypatch.setattr(fcntl, "flock", interrupted)
    with pytest.raises(KeyboardInterrupt), projectcli._manifest_lock(manifest):
        pytest.fail("the lock must not be granted")
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptors[0])
