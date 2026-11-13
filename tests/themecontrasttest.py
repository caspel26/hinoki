"""WCAG contrast of every theme's text/background pairs, light and dark."""

import re

import pytest

from hinoki.admin.themes import THEMES, Theme

AA = 4.5
HEX = re.compile(r"#[0-9a-fA-F]{6}")
PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text", "sidebar-bg"),
    ("text", "hover"), ("muted", "bg"), ("muted", "surface"), ("muted", "sidebar-bg"),
    ("accent-ink", "accent"), ("accent-ink", "accent-hover"), ("accent-text", "bg"),
    ("accent-text", "surface"), ("accent-soft-ink", "accent-soft"),
    ("ok-ink", "ok-bg"), ("warn-ink", "warn-bg"), ("danger-ink", "danger-bg"),
    ("danger-on", "danger"), ("avatar-ink", "avatar-bg"), ("text", "accent-soft"),
]  # fmt: skip


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def colors(value: str) -> list[str]:
    """The hex colours in a token (a gradient has several stops)."""
    return HEX.findall(value)


def failures(theme: Theme, scheme: str) -> list[str]:
    tokens = theme.light if scheme == "light" else theme.dark
    found = []
    for foreground, background in PAIRS:
        for fg in colors(tokens[foreground]):
            for bg in colors(tokens[background]):
                ratio = contrast(fg, bg)
                if ratio < AA:
                    found.append(f"{foreground} {fg} on {background} {bg}: {ratio:.2f}")
    return found


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("name", sorted(THEMES))
def test_every_pair_reaches_wcag_aa(name: str, scheme: str) -> None:
    assert failures(THEMES[name], scheme) == []


def test_the_formula_matches_published_values() -> None:
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#777777", "#ffffff"), 2) == 4.48  # the well-known near miss
    assert contrast("#123456", "#123456") == 1.0
