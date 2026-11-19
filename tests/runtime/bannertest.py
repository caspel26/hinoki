from __future__ import annotations

import sys

import pytest

from hinoki.runtime import banner
from hinoki.settings import ServerSettings


def line(settings: ServerSettings, *, colour: bool, **overrides: object) -> str:
    options = {"dev": False, "workers": 1, "scheme": "http", **overrides}
    return banner.banner(settings, version="1.2.3", colour=colour, **options)  # type: ignore[arg-type]


def test_a_plain_banner_is_one_line_with_the_facts() -> None:
    text = line(ServerSettings(), colour=False, workers=4, scheme="https")
    assert text == (
        "Kiso 1.2.3 (Hinoki's Rust server): serve, 4 workers, https, "
        "access log off, native lane on"
    )


def test_dev_turns_the_access_log_on_and_the_settings_turn_the_lane_off() -> None:
    settings = ServerSettings(native_dispatch=False)
    text = line(settings, colour=False, dev=True)
    assert "dev, reloads on file changes, 1 worker" in text
    assert "access log on, native lane off" in text
    assert "access log off" in line(
        ServerSettings(access_log=False), colour=False, dev=True
    )


def test_a_coloured_banner_names_kiso_in_bold_and_keeps_the_facts() -> None:
    text = line(ServerSettings(), colour=True)
    assert text.startswith("\x1b[1;36mKiso 1.2.3\x1b[0m")
    assert "serve \u00b7 1 worker \u00b7 http" in text
    assert text.count("\n") == 1


def test_colour_needs_a_terminal_and_no_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert banner.wants_colour() is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert banner.wants_colour() is False
