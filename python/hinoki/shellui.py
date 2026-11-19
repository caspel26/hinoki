"""Banner, prompts and exit line for ``hinoki shell`` on a capable terminal."""

from __future__ import annotations

from collections.abc import Collection

from hinoki import __version__
from hinoki.terminal import Style

PLAIN_GREETING = "Hinoki interactive shell; use Ctrl-D or Ctrl-C to exit."
PLAIN_PROMPTS = (">>> ", "... ")
SHOWN_MODELS = 6


def prompts(style: Style) -> tuple[str, str]:
    """The primary and continuation prompts; Python's own when plain."""
    if not style.fancy:
        return PLAIN_PROMPTS
    primary = (
        f"{style.paint('hinoki', 'brand', bold=True)} {style.paint('\u276f', 'brand')} "
    )
    return primary, style.dim("  \u00b7\u00b7\u00b7 ")


def model_summary(style: Style, names: Collection[str]) -> str:
    """The first few model names, then how many more there are."""
    ordered = sorted(names)
    if not ordered:
        return style.dim("none")
    shown = ", ".join(ordered[:SHOWN_MODELS])
    extra = len(ordered) - SHOWN_MODELS
    more = style.dim(f"  +{extra} more") if extra > 0 else ""
    return f"{style.text(shown)}{more}"


def row(style: Style, label: str, value: str) -> str:
    return style.line("rail", f"{style.dim(f'{label:<10}')}{value}")


def greeting(
    style: Style, title: str, settings: str, models: Collection[str]
) -> list[str]:
    """The lines printed before the first prompt."""
    if not style.fancy:
        return [PLAIN_GREETING]
    return [
        style.banner("interactive shell", f"v{__version__}"),
        style.line("start", style.bold("shell") + style.dim("  ctrl-d to exit")),
        style.line("rail"),
        row(style, "app", style.text(title)),
        row(style, "settings", style.text(settings)),
        row(style, "models", model_summary(style, models)),
        style.line("rail"),
        style.line(
            "end",
            style.dim("top-level ")
            + style.paint("await", "brand")
            + style.dim(" works; ctrl-c interrupts a running cell"),
        ),
        "",
    ]


def farewell(style: Style) -> str:
    """Moves the cursor off the prompt line when the session ends."""
    return style.dim("bye") if style.fancy else ""
