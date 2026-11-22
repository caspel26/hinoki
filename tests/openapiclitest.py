"""``hinoki openapi`` output, file and drift-check behavior."""

import json
from pathlib import Path

import pytest
from openapi_spec_validator import validate

from hinoki.__main__ import main

TARGET = "tests.routeinclusiontest:make_app"


def test_command_prints_deterministic_valid_document_without_a_served_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["openapi", TARGET]) == 0
    first = capsys.readouterr().out
    assert first.endswith("\n")
    document = json.loads(first)
    assert document["openapi"] == "3.2.0"
    validate(document)

    assert main(["openapi", TARGET]) == 0
    assert capsys.readouterr().out == first


def test_output_writes_deterministic_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "openapi.json"
    assert main(["openapi", TARGET, "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    contents = output.read_text(encoding="utf-8")
    assert contents.endswith("\n")
    assert json.loads(contents)["openapi"] == "3.2.0"
    assert contents == json.dumps(json.loads(contents), indent=2, sort_keys=True) + "\n"


def test_check_ignores_json_formatting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "openapi.json"
    assert main(["openapi", TARGET, "--output", str(output)]) == 0
    output.write_text(json.dumps(json.loads(output.read_text())), encoding="utf-8")

    assert main(["openapi", TARGET, "--check", str(output)]) == 0
    assert capsys.readouterr().out == ""


def test_check_reports_drift_and_recommends_output_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "openapi.json"
    output.write_text("{}", encoding="utf-8")

    assert main(["openapi", TARGET, "--check", str(output)]) == 1
    error = capsys.readouterr().err
    assert "is out of date" in error
    assert f"hinoki openapi {TARGET} --output {output}" in error


def test_check_returns_two_for_missing_or_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"
    assert main(["openapi", TARGET, "--check", str(missing)]) == 2
    assert "does not exist" in capsys.readouterr().err

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    assert main(["openapi", TARGET, "--check", str(invalid)]) == 2
    assert "cannot read JSON" in capsys.readouterr().err


def test_invalid_target_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["openapi", "missing.module:app"]) == 2
    assert "cannot load missing.module:app" in capsys.readouterr().err
