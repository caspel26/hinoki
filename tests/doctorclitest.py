"""Project doctor tests: output stability and non-invasive execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hinoki import Hinoki, ModuleContext, __main__
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
        "hinoki.doctorcli.build_app",
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
        "hinoki.doctorcli.build_app",
        lambda _target: (_ for _ in ()).throw(ValueError("secret setting value")),
    )

    code = __main__.main(["doctor", "missing:app", "--format", "json"])
    output = capsys.readouterr().out
    result = json.loads(output)
    check = next(item for item in result["checks"] if item["id"] == "application.load")

    assert code == 2
    assert check["status"] == "failed"
    assert "secret setting value" not in output


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

    monkeypatch.setattr("hinoki.doctorcli.build_app", build)
    code = __main__.main(["doctor", "example:app"])

    assert code == 0
    assert calls == 0
    capsys.readouterr()


def test_doctor_reports_typed_settings_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def build(_target: str) -> Hinoki:
        return Hinoki(settings=AppSettings())

    monkeypatch.setattr("hinoki.doctorcli.build_app", build)
    code = __main__.main(["doctor", "example:app", "--format", "json"])
    output = capsys.readouterr().out
    result = json.loads(output)
    settings = next(
        item for item in result["checks"] if item["id"] == "application.settings"
    )

    assert code == 0
    assert settings["status"] == "ok"
    assert "timezone" not in output
