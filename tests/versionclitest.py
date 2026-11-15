"""The version command reports only metadata available in this installation."""

import json
from importlib import metadata
from importlib.metadata import PackageNotFoundError

import pytest

import hinoki
from hinoki.__main__ import main


def test_default_output_is_the_hinoki_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out == f"hinoki {hinoki.__version__}\n"


def test_json_output_and_verbose_runtime_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["version", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"hinoki": hinoki.__version__}

    assert main(["version", "--verbose", "--format", "json"]) == 0
    details = json.loads(capsys.readouterr().out)
    assert details["hinoki"] == hinoki.__version__
    assert details["python"]
    assert details["platform"]
    assert details["core"].endswith((".so", ".pyd", ".dylib"))
    assert details["granian"]
    assert "sqlite" not in details

    assert main(["version", "--verbose"]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"hinoki {hinoki.__version__}\n")
    assert "Python:" in text
    assert "Platform:" in text
    assert "Native core:" in text
    assert "Granian:" in text


def test_verbose_output_handles_missing_optional_granian(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed_version = metadata.version

    def missing_granian(name: str) -> str:
        if name == "granian":
            raise PackageNotFoundError(name)
        return installed_version(name)

    monkeypatch.setattr(metadata, "version", missing_granian)
    assert main(["version", "--verbose"]) == 0
    assert "Granian: not installed" in capsys.readouterr().out
