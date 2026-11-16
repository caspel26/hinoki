"""Settings command output and target-resolution boundaries."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from typing import Any

import pytest

from hinoki import settingscli
from hinoki.settings import Settings, SettingsError


class ExampleSettings(Settings):
    label: str = "Hinoki"
    retries: int = 3


def test_load_class_reports_import_and_usage_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(_target: str) -> Any:
        raise ImportError("module absent")

    monkeypatch.setattr(settingscli, "load_target", fail_import)
    with pytest.raises(SystemExit, match="cannot load sample:Settings"):
        settingscli.load_class("sample:Settings")

    monkeypatch.setattr(settingscli, "load_target", lambda _target: object())

    def fail_build(_target: str) -> Any:
        raise SystemExit("invalid app")

    monkeypatch.setattr(settingscli, "build_app", fail_build)
    with pytest.raises(SystemExit, match="not a Settings class or an app"):
        settingscli.load_class("sample:app")


def test_load_class_accepts_settings_types_and_typed_app_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settingscli, "load_target", lambda _target: ExampleSettings)
    assert settingscli.load_class("sample:ExampleSettings") is ExampleSettings

    monkeypatch.setattr(settingscli, "load_target", lambda _target: object())
    monkeypatch.setattr(
        settingscli,
        "build_app",
        lambda _target: SimpleNamespace(settings=ExampleSettings()),
    )
    assert settingscli.load_class("sample:app") is ExampleSettings

    monkeypatch.setattr(
        settingscli, "build_app", lambda _target: SimpleNamespace(settings=None)
    )
    with pytest.raises(SystemExit, match="has no typed settings"):
        settingscli.load_class("sample:untyped_app")


def test_run_settings_prints_example_text_json_and_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settingscli, "load_class", lambda _target: ExampleSettings)
    args = argparse.Namespace(
        target="sample:ExampleSettings",
        example=True,
        prefix=None,
        dotenv=None,
        toml=None,
        format="text",
    )
    assert settingscli.run_settings(args) == 0
    assert "LABEL=Hinoki" in capsys.readouterr().out

    args.example = False
    args.format = "json"
    assert settingscli.run_settings(args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["key"] for row in rows} == {"LABEL", "RETRIES"}
    assert all(set(row) == {"key", "value", "origin"} for row in rows)

    args.format = "text"
    assert settingscli.run_settings(args) == 0
    output = capsys.readouterr().out
    assert "LABEL" in output and "default" in output


def test_run_settings_reports_validation_without_setting_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settingscli, "load_class", lambda _target: ExampleSettings)

    def invalid(**_options: Any) -> list[Any]:
        raise SettingsError(["BROKEN is invalid"])

    monkeypatch.setattr(ExampleSettings, "explain", invalid)
    args = argparse.Namespace(
        target="sample:ExampleSettings",
        example=False,
        prefix=None,
        dotenv=None,
        toml=None,
        format="text",
    )
    assert settingscli.run_settings(args) == 1
    error = capsys.readouterr().err
    assert "BROKEN is invalid" in error
    assert "rerun `hinoki settings`" in error
