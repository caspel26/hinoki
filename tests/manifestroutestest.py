"""Routes declared in hinoki.toml are bound by the app that the manifest names."""

import importlib
import re
import sys
from pathlib import Path

import pytest

from hinoki import Hinoki
from hinoki.project import ProjectError, load_manifest, manifest_routes

FEATURE = """from hinoki import http


@http.get("/")
def listing() -> dict[str, str]:
    return {{"app": "{name}"}}


routes = [listing]
"""
HEALTH = """from hinoki import http


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


routes = [health]
"""
APP = """from hinoki import Hinoki

app = Hinoki()
"""
MANIFEST = """[project]
application = "{package}.app:app"
routes = "{package}.routes:routes"

[[apps]]
name = "posts"
models = "{package}.posts.models"
migrations = "{package}.posts.migrations"
routes = "{package}.posts.routes:routes"
requires = []

[[apps]]
name = "billing"
models = "{package}.billing.models"
migrations = "{package}.billing.migrations"
routes = "{package}.billing.routes:routes"
prefix = "/invoicing"
requires = ["posts"]
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    package = f"manifestproject{abs(hash(tmp_path)) % 10**8}"
    root = tmp_path / package
    write(tmp_path / "hinoki.toml", MANIFEST.format(package=package))
    write(root / "__init__.py", "")
    write(root / "routes.py", HEALTH)
    write(root / "app.py", APP)
    for name in ("posts", "billing"):
        write(root / name / "__init__.py", "")
        write(root / name / "routes.py", FEATURE.format(name=name))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield_name = package
    for name in [key for key in sys.modules if key.startswith(package)]:
        monkeypatch.delitem(sys.modules, name)
    return tmp_path, yield_name


def paths(app: Hinoki) -> list[str]:
    return [route.path for route in app.routes]


def test_the_app_the_manifest_names_binds_the_project_and_app_routes(
    project: tuple[Path, str],
) -> None:
    _, package = project
    app = importlib.import_module(f"{package}.app").app
    assert paths(app) == ["/health", "/posts", "/invoicing"]


def test_an_explicit_include_wins_over_the_manifest(
    project: tuple[Path, str],
) -> None:
    root, package = project
    write(
        root / package / "app.py",
        f"from hinoki import Hinoki\nfrom {package}.routes import health\n\n"
        "app = Hinoki(include=[health])\nempty = Hinoki(include=[])\n",
    )
    module = importlib.import_module(f"{package}.app")
    assert paths(module.app) == ["/health"]
    assert paths(module.empty) == []


def test_an_application_in_another_module_is_not_bound(
    project: tuple[Path, str],
) -> None:
    root, package = project
    write(root / package / "other.py", APP)
    assert paths(importlib.import_module(f"{package}.other").app) == []
    assert paths(Hinoki()) == []


def test_a_manifest_without_routes_changes_nothing(
    project: tuple[Path, str],
) -> None:
    root, package = project
    write(
        root / "hinoki.toml",
        f'[project]\napplication = "{package}.app:app"\n\n[[apps]]\nname = "posts"\n'
        f'models = "{package}.posts.models"\n'
        f'migrations = "{package}.posts.migrations"\n',
    )
    assert paths(importlib.import_module(f"{package}.app").app) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ('routes = "{package}.missing:routes"', "cannot load the routes of project"),
        ('routes = "{package}.routes:nothing"', "cannot load the routes of project"),
        ('routes = "{package}.posts.routes:listing"', "must be a list of routes"),
    ],
)
def test_unusable_route_targets_fail_when_the_app_is_built(
    project: tuple[Path, str], change: str, message: str
) -> None:
    root, package = project
    text = (root / "hinoki.toml").read_text(encoding="utf-8")
    original = f'routes = "{package}.routes:routes"'
    write(root / "hinoki.toml", text.replace(original, change.format(package=package)))
    with pytest.raises(ProjectError, match=message):
        importlib.import_module(f"{package}.app")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("routes = 3", "project.routes must be a module:attribute target"),
        ('routes = "nothing"', "project.routes must be a module:attribute target"),
    ],
)
def test_project_routes_must_be_a_target(
    tmp_path: Path, text: str, message: str
) -> None:
    write(
        tmp_path / "hinoki.toml",
        f'[project]\napplication = "a.main:app"\n{text}\n',
    )
    with pytest.raises(ProjectError, match=message):
        load_manifest(tmp_path / "hinoki.toml")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('routes = "bad"', "routes must be a module:attribute target"),
        ("routes = 4", "routes must be a module:attribute target"),
        ('prefix = "billing"', "prefix must start with"),
        ('prefix = "/billing/"', "prefix must start with"),
        ("prefix = 5", "prefix must start with"),
    ],
)
def test_app_routes_and_prefix_are_checked(
    tmp_path: Path, extra: str, message: str
) -> None:
    write(
        tmp_path / "hinoki.toml",
        '[project]\napplication = "a.main:app"\n\n[[apps]]\nname = "billing"\n'
        f'models = "a.models"\nmigrations = "a.migrations"\n{extra}\n',
    )
    with pytest.raises(ProjectError, match=message):
        load_manifest(tmp_path / "hinoki.toml")


def test_a_malformed_manifest_is_ignored_by_an_unrelated_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "hinoki.toml", "this is not toml")
    write(tmp_path / "plain.py", "from hinoki import Hinoki\napp = Hinoki()\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "plain", raising=False)
    assert paths(importlib.import_module("plain").app) == []


def test_manifest_routes_reads_the_manifest_above_the_calling_file(
    project: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, package = project
    write(
        root / package / "app.py",
        "from hinoki import Hinoki\nfrom hinoki.project import manifest_routes\n\n"
        "app = Hinoki(include=manifest_routes())\n",
    )
    monkeypatch.chdir(root.parent)
    app = importlib.import_module(f"{package}.app").app
    assert paths(app) == ["/health", "/posts", "/invoicing"]


def test_manifest_routes_from_an_explicit_start_directory(
    project: tuple[Path, str],
) -> None:
    root, package = project
    app = Hinoki(include=manifest_routes(root / package / "posts"))
    assert paths(app) == ["/health", "/posts", "/invoicing"]


def test_manifest_routes_outside_a_project_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    write(
        outside / "loose.py",
        "from hinoki.project import manifest_routes\n\nroutes = manifest_routes()\n",
    )
    monkeypatch.syspath_prepend(str(outside))
    monkeypatch.delitem(sys.modules, "loose", raising=False)
    missing = rf"no hinoki\.toml found in {re.escape(str(outside))} or above"
    with pytest.raises(ProjectError, match=missing):
        importlib.import_module("loose")
    with pytest.raises(ProjectError, match=missing):
        manifest_routes(outside)
