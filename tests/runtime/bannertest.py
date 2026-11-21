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
    ("environment", "kiso"),
    [
        ({"TERM": "xterm"}, "\x1b[1;35mK"),
        ({"COLORTERM": "truecolor"}, "\x1b[1;38;2;185;160;231mK"),
    ],
)
def test_a_coloured_banner_is_the_hinoki_box_titled_kiso_without_a_mark(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], kiso: str
) -> None:
    for name in ("TERM", "COLORTERM", "COLORFGBG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("COLUMNS", "80")
    text = line(ServerSettings(), colour=True)
    rows = [ANSI.sub("", row) for row in text.strip("\n").split("\n")]

    assert rows[0].startswith("  \u256d\u2500 Kiso \u2500")
    assert rows[-1] == "  \u2570" + "\u2500" * (len(rows[0]) - 4) + "\u256f"
    assert {len(row) for row in rows} == {len(rows[0])}
    assert "\u25e2" not in text and "\u25e4" not in text and "\x1b_G" not in text
    inside = "\n".join(rows[1:-1])
    assert "Hinoki's Rust server" in inside and "1.2.3" in inside
    assert "serve \u00b7 1 worker \u00b7 http \u00b7 access log off" in inside
    assert "native lane on" in inside


def test_the_facts_break_into_rows_that_fit_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "50")
    text = line(ServerSettings(), colour=True, dev=True, workers=4)
    rows = [ANSI.sub("", row) for row in text.strip("\n").split("\n")]

    assert {len(row) for row in rows} == {len(rows[0])} and len(rows[0]) <= 50
    assert "\u2026" not in text
    assert sum("\u00b7" in row for row in rows) >= 2


def test_wrapped_keeps_every_fact_whole() -> None:
    rows = banner.wrapped(["alpha", "beta", "gamma"], 12)
    assert rows == ["alpha \u00b7 beta", "gamma"]


def test_colour_needs_a_terminal_and_no_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    assert banner.wants_colour() is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert banner.wants_colour() is False
