"""Project doctor tests: output stability and non-invasive execution."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest

from hinoki import Hinoki, ModuleContext, __main__
from hinoki.errors import RegistrationError
from hinoki.settings import AppSettings


def test_doctor_without_target_reports_environment_and_skips_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    code = __main__.main(["doctor", "--format", "json"])
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["schema_version"] == 1
    checks = {item["id"]: item for item in result["checks"]}
    assert checks["project.manifest"]["status"] == "skipped"
    assert checks["environment.python"]["status"] == "ok"
    assert checks["application.load"]["status"] == "skipped"
    assert "value" not in str(result)


def test_doctor_loads_manifest_default_and_generates_openapi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hinoki.toml").write_text(
        '[project]\napplication = "tests.projects.inherited:create_app"\n',
        encoding="utf-8",
    )
    code = __main__.main(["doctor"])
    output = capsys.readouterr().out

    assert code == 0
    assert "project.manifest" in output
    assert "application.routes" in output
    assert "application.openapi" in output


def test_doctor_invalid_manifest_does_not_import_default_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hinoki.toml").write_text("invalid = [", encoding="utf-8")
    monkeypatch.setattr(
        "hinoki.cli.doctor.build_app",
        lambda _target: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    code = __main__.main(["doctor"])
    output = capsys.readouterr().out

    assert code == 1
    assert "project.manifest" in output
    assert "manifest validation failed" in output


def test_doctor_explicit_target_reports_load_error_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "hinoki.cli.doctor.build_app",
        lambda _target: (_ for _ in ()).throw(ValueError("secret setting value")),
    )

    code = __main__.main(["doctor", "missing:app", "--format", "json"])
    output = capsys.readouterr().out
    result = json.loads(output)
    check = next(item for item in result["checks"] if item["id"] == "application.load")

    assert code == 2
    assert check["status"] == "failed"
    assert "secret setting value" not in output
    assert "inspect it with `hinoki routes TARGET`" in check["remediation"]


def test_doctor_load_error_shows_hinoki_declaration_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "hinoki.cli.doctor.build_app",
        lambda _target: (_ for _ in ()).throw(
            RegistrationError("admin: no secret\nset <PREFIX>_ADMIN__SECRET")
        ),
    )

    code = __main__.main(["doctor", "missing:app", "--format", "json"])
    output = capsys.readouterr().out
    result = json.loads(output)
    check = next(item for item in result["checks"] if item["id"] == "application.load")

    assert code == 2
    assert "RegistrationError: admin: no secret set <PREFIX>_ADMIN__SECRET" in output
    assert "message above gives the cause" in check["remediation"]
    assert "inspect it with" not in check["remediation"]


def test_doctor_hides_the_message_of_a_chained_declaration_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def build(_target: str) -> None:
        try:
            raise ValueError("secret setting value")
        except ValueError as cause:
            raise RegistrationError("bad declaration") from cause

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hinoki.cli.doctor.build_app", build)

    code = __main__.main(["doctor", "missing:app", "--format", "json"])
    output = capsys.readouterr().out
    checks = json.loads(output)["checks"]
    check = next(item for item in checks if item["id"] == "application.load")

    assert code == 2
    assert "secret setting value" not in output
    assert "bad declaration" not in output
    assert "inspect it with `hinoki routes TARGET`" in check["remediation"]


def test_doctor_does_not_connect_or_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor must not connect or write")

    monkeypatch.setattr("hinoki.db.Database.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.write_text", forbidden)
    code = __main__.main(["doctor", "tests.projects.inherited:create_app"])

    assert code == 0
    assert "Database" in capsys.readouterr().out


def test_doctor_does_not_start_application_hooks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def startup(_context: ModuleContext) -> None:
        nonlocal calls
        calls += 1

    def build(_target: str) -> Hinoki:
        return Hinoki(on_startup=[startup])

    monkeypatch.setattr("hinoki.cli.doctor.build_app", build)
    code = __main__.main(["doctor", "example:app"])

    assert code == 0
    assert calls == 0
    capsys.readouterr()


def test_doctor_reports_typed_settings_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def build(_target: str) -> Hinoki:
        return Hinoki(settings=AppSettings())

    monkeypatch.setattr("hinoki.cli.doctor.build_app", build)
    code = __main__.main(["doctor", "example:app", "--format", "json"])
    output = capsys.readouterr().out
    result = json.loads(output)
    settings = next(
        item for item in result["checks"] if item["id"] == "application.settings"
    )

    assert code == 0
    assert settings["status"] == "ok"
    assert "timezone" not in output


def test_doctor_warns_when_granian_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr("hinoki.cli.doctor.importlib.metadata.version", missing)
    code = __main__.main(
        ["doctor", "tests.projects.inherited:create_app", "--format", "json"]
    )
    result = json.loads(capsys.readouterr().out)
    check = next(
        item for item in result["checks"] if item["id"] == "environment.server"
    )

    assert code == 0
    assert check["status"] == "warning"


def test_doctor_reports_openapi_generation_failure_without_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def broken(_self: Hinoki) -> dict[str, object]:
        raise RuntimeError("secret schema detail")

    monkeypatch.setattr(Hinoki, "openapi", broken)
    code = __main__.main(
        ["doctor", "tests.projects.inherited:create_app", "--format", "json"]
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    check = next(
        item for item in result["checks"] if item["id"] == "application.openapi"
    )

    assert code == 1
    assert check["status"] == "failed"
    assert "secret schema detail" not in output


def test_doctor_warns_for_non_hinoki_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = SimpleNamespace(settings=object(), routes=[], openapi=lambda: {})
    monkeypatch.setattr("hinoki.cli.doctor.build_app", lambda _target: app)

    code = __main__.main(["doctor", "example:app", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    check = next(
        item for item in result["checks"] if item["id"] == "application.settings"
    )

    assert code == 0
    assert check["status"] == "warning"
