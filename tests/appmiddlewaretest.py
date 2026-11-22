"""Apps declare their middleware in ``hinoki.toml``."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hinoki import Hinoki
from hinoki.__main__ import main
from hinoki.project import ProjectError, load_manifest, manifest_middleware
from hinoki.scaffold import create

_PROJECT_PACKAGES = {"sample", "legacy", "wrongpkg", "badpkg"}

_MODULE = """from hinoki.http import Middleware


class {name}(Middleware):
    pass


middleware = [{name}()]
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = create("sample", tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.syspath_prepend(str(root))
    yield root
    for module in [m for m in sys.modules if m.split(".")[0] in _PROJECT_PACKAGES]:
        del sys.modules[module]


def _write_middleware(project: Path, app: str, name: str) -> None:
    path = project / "sample" / "apps" / app / "middleware.py"
    path.write_text(_MODULE.format(name=name), encoding="utf-8")


def test_manifest_middleware_flattens_in_dependency_order(project: Path) -> None:
    # "alpha" sorts first but requires "zeta", so zeta's middleware comes first.
    assert main(["app", "create", "zeta", "--no-input"]) == 0
    assert main(["app", "create", "alpha", "--requires", "zeta", "--no-input"]) == 0
    assert main(["app", "create", "plain", "--no-input"]) == 0
    _write_middleware(project, "zeta", "Zeta")
    _write_middleware(project, "alpha", "Alpha")
    manifest = project / "hinoki.toml"
    text = manifest.read_text(encoding="utf-8")
    for app in ("zeta", "alpha"):
        text = text.replace(
            f'name = "{app}"\n',
            f'name = "{app}"\nmiddleware = "sample.apps.{app}.middleware:middleware"\n',
        )
    manifest.write_text(text, encoding="utf-8")

    found = manifest_middleware(project)

    assert [type(item).__name__ for item in found] == ["Zeta", "Alpha"]
    assert Hinoki(middleware=found).middleware == tuple(found)


def test_add_and_list_show_the_middleware_target(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "app",
                "add",
                "billing",
                "--models",
                "legacy.billing.models",
                "--middleware",
                "legacy.billing.middleware:middleware",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["app", "list"]) == 0
    assert "middleware: legacy.billing.middleware:middleware" in (
        capsys.readouterr().out
    )

    assert main(["app", "list", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)["apps"]
    billing = next(row for row in rows if row["name"] == "billing")
    assert billing["middleware"] == "legacy.billing.middleware:middleware"


def test_an_unresolvable_target_names_the_app(project: Path) -> None:
    main(["app", "add", "a", "--models", "nope.models", "--middleware", "nope.m:x"])

    with pytest.raises(ProjectError, match="cannot load the middleware of app 'a'"):
        manifest_middleware(project)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("middleware = 1\n", "must be a list or tuple of Middleware"),
        ("middleware = [object()]\n", "async def __call__"),
    ],
)
def test_a_wrong_type_target_names_the_app(
    project: Path, body: str, message: str
) -> None:
    package = project / "badpkg"
    package.mkdir()
    (package / "__init__.py").write_text(body, encoding="utf-8")
    main(
        [
            "app",
            "add",
            "b",
            "--models",
            "badpkg.models",
            "--middleware",
            "badpkg:middleware",
        ]
    )

    with pytest.raises(ProjectError, match=f"middleware of app 'b'.*{message}"):
        manifest_middleware(project)


def test_a_non_string_target_is_rejected_at_load(project: Path) -> None:
    manifest = project / "hinoki.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[apps]]\nname = "a"\nmodels = "sample.models"\n'
        + 'migrations = "sample.migrations"\nmiddleware = ["x:y"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="middleware must be a module:attribute"):
        load_manifest()
