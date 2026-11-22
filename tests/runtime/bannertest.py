from __future__ import annotations

import sys

import pytest

from hinoki.runtime import banner
from hinoki.settings import ServerSettings
from hinoki.terminal import ANSI


def line(settings: ServerSettings, *, colour: bool, **overrides: object) -> str:
    style = banner.brand_style() if colour else None
    options = {"dev": False, "workers": 1, "scheme": "http", **overrides}
    return banner.banner(settings, version="1.2.3", style=style, **options)  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    ("environment", "title"),
    [
        ({"TERM": "xterm"}, "\x1b[1;39mKiso"),
        ({"COLORTERM": "truecolor"}, "\x1b[1;38;2;238;237;242mKiso"),
    ],
)
def test_a_coloured_banner_is_the_kiso_branch_with_the_tagline_and_the_facts(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], title: str
) -> None:
    for name in ("TERM", "COLORTERM", "COLORFGBG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("COLUMNS", "80")
    text = line(ServerSettings(), colour=True)
    rows = [ANSI.sub("", row) for row in text.strip("\n").split("\n")]

    assert title in text and "\x1b_G" not in text
    assert rows == [
        "  \u256d\u2500 Kiso  Hinoki's Rust server  v1.2.3",
        "  \u2502",
        "  \u2502  mode         serve",
        "  \u2502  workers      1",
        "  \u2502  scheme       http",
        "  \u2502  access log   off",
        "  \u2502  native lane  on",
        "  \u2570\u2500",
    ]


def test_a_narrow_terminal_wraps_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "44")
    text = line(ServerSettings(), colour=True, dev=True, workers=4)
    rows = [ANSI.sub("", row) for row in text.strip("\n").split("\n")]

    assert max(len(row) for row in rows) <= 43
    assert not any("file changes" in row and "reloads" in row for row in rows)
    assert any(row.endswith("changes") for row in rows)
    assert "\u2026" not in text


def test_colour_needs_a_terminal_and_no_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert banner.wants_colour() is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert banner.wants_colour() is False
