"""Admin themes: a theme is a set of design tokens plus two layout choices.

Every component template and ``ui/base.css`` use only ``--hn-*`` custom
properties, so a theme restyles the whole admin by defining the tokens below
for a light and a dark scheme (plus optional extra CSS). ``edit`` chooses where
the edit form opens: ``"drawer"`` (beside the list) or ``"page"``.
A theme whose ``default_mode`` is ``"dark"`` is dark unless the user switches.

Built in, three deliberately different looks: ``studio`` (default: soft violet,
rounded, edit drawer), ``midnight`` (dark, neon cyan, monospaced labels) and
``paper`` (warm editorial: serif headings, terracotta, hairlines, edit page).
To make your own, build a ``Theme`` and pass it as ``Admin(theme=...)``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

COLOR_TOKENS = (
    "bg", "surface", "surface-2", "sidebar-bg", "topbar-bg", "text", "muted", "line",
    "line-strong", "hover", "accent", "accent-hover", "accent-ink", "accent-text",
    "accent-soft", "accent-soft-ink", "ok-bg", "ok-ink", "warn-bg", "warn-ink",
    "danger", "danger-on", "danger-bg", "danger-ink", "focus", "avatar-bg",
    "avatar-ink", "shadow",
)  # fmt: skip
FIXED_TOKENS = (
    "font", "font-size", "radius", "radius-sm", "radius-lg", "nav-pad", "cell-pad",
    "h1", "sidebar-w", "drawer-w",
)  # fmt: skip
SYSTEM_FONT = (
    'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif'
)


@dataclasses.dataclass(frozen=True, slots=True)
class Theme:
    name: str
    label: str
    edit: str  # "drawer" or "page"
    light: Mapping[str, str]
    dark: Mapping[str, str]
    fixed: Mapping[str, str]
    default_mode: str = "light"
    extra_css: str = ""

    def check(self) -> None:
        """Fail at configuration time, not on a half-styled page."""
        for scheme, tokens in (("light", self.light), ("dark", self.dark)):
            missing = [t for t in COLOR_TOKENS if t not in tokens]
            if missing:
                raise ValueError(f"theme {self.name!r}: {scheme} lacks {missing}")
        missing = [t for t in FIXED_TOKENS if t not in self.fixed]
        if missing:
            raise ValueError(f"theme {self.name!r}: fixed tokens lack {missing}")
        if self.edit not in {"drawer", "page"} or self.default_mode not in {
            "light",
            "dark",
        }:
            raise ValueError(f"theme {self.name!r}: bad edit or default_mode")
        if not self.name.replace("-", "").isalnum():
            raise ValueError("theme names are letters, digits and dashes")

    def declarations(self, tokens: Mapping[str, str]) -> str:
        return "".join(f"--hn-{key}:{value};" for key, value in tokens.items())

    def tokens_css(self) -> str:
        """The token blocks for both schemes, honouring the system preference,
        an explicit ``data-mode`` and the theme's default mode."""
        me = f':root[data-theme="{self.name}"]'
        fixed = self.declarations(self.fixed)
        first, second = (
            (self.light, self.dark)
            if self.default_mode == "light"
            else (self.dark, self.light)
        )
        first_mode, second_mode = (
            ("light", "dark") if self.default_mode == "light" else ("dark", "light")
        )
        css = f"{me}{{{fixed}{self.declarations(first)}color-scheme:{first_mode};}}\n"
        if self.default_mode == "light":
            css += (
                f"@media (prefers-color-scheme: dark){{{me}:not([data-mode=light])"
                f"{{{self.declarations(second)}color-scheme:dark;}}}}\n"
            )
        css += f"{me}[data-mode={second_mode}]{{{self.declarations(second)}color-scheme:{second_mode};}}\n"
        css += f"{me}[data-mode={first_mode}]{{{self.declarations(first)}color-scheme:{first_mode};}}\n"
        return css + self.extra_css


STUDIO_LIGHT = {
    "bg": "#f7f4fc", "surface": "#ffffff", "surface-2": "#f1ecf9",
    "sidebar-bg": "linear-gradient(180deg,#efe7fb,#f8f5fd)", "topbar-bg": "rgba(255,255,255,.88)",
    "text": "#1b1230", "muted": "#675c7a", "line": "#e7dff3", "line-strong": "#cdbfe3",
    "hover": "#f3eefa", "accent": "#6d3fc4", "accent-hover": "#5b32ad", "accent-ink": "#ffffff",
    "accent-text": "#5b32ad", "accent-soft": "#e9defb", "accent-soft-ink": "#4a2398",
    "ok-bg": "#dcf5e5", "ok-ink": "#17663a", "warn-bg": "#fde9cf", "warn-ink": "#8a4b06",
    "danger": "#c42b2b", "danger-on": "#ffffff", "danger-bg": "#fde3e3", "danger-ink": "#a01c1c",
    "focus": "#2563eb", "avatar-bg": "#c9b3f3", "avatar-ink": "#3d1d85",
    "shadow": "0 1px 2px rgba(60,30,110,.06)",
}  # fmt: skip
STUDIO_DARK = {
    "bg": "#120b1c", "surface": "#1c1228", "surface-2": "#261a36",
    "sidebar-bg": "linear-gradient(180deg,#1a1026,#140c1f)", "topbar-bg": "rgba(28,18,40,.88)",
    "text": "#f1ebfa", "muted": "#b3a6c8", "line": "#33244a", "line-strong": "#4a3866",
    "hover": "#251a35", "accent": "#b48cf5", "accent-hover": "#c4a3f8", "accent-ink": "#1a0f2a",
    "accent-text": "#c9aaf8", "accent-soft": "#34234f", "accent-soft-ink": "#dcc8fb",
    "ok-bg": "#17382a", "ok-ink": "#7fe0a6", "warn-bg": "#3e2d10", "warn-ink": "#f3c27a",
    "danger": "#ff8a8a", "danger-on": "#2a0b0b", "danger-bg": "#43191b", "danger-ink": "#ffb0b0",
    "focus": "#79b8ff", "avatar-bg": "#4a3380", "avatar-ink": "#e9defb", "shadow": "none",
}  # fmt: skip
NEUTRAL_LIGHT = {
    "bg": "#f4f5f7", "surface": "#ffffff", "surface-2": "#eceef1", "sidebar-bg": "#ffffff",
    "topbar-bg": "#ffffff", "text": "#1c2430", "muted": "#566070", "line": "#dde1e6",
    "line-strong": "#b8c0ca", "hover": "#f0f2f5", "accent": "#1f5fa8", "accent-hover": "#174a85",
    "accent-ink": "#ffffff", "accent-text": "#174a85", "accent-soft": "#dbe8f7",
    "accent-soft-ink": "#123e72", "ok-bg": "#dff3e4", "ok-ink": "#18633a", "warn-bg": "#fbebd0",
    "warn-ink": "#7a4a05", "danger": "#b42318", "danger-on": "#ffffff", "danger-bg": "#fde7e5",
    "danger-ink": "#912018", "focus": "#1a73e8", "avatar-bg": "#c5d6ea", "avatar-ink": "#17406e",
    "shadow": "none",
}  # fmt: skip
PAPER_LIGHT = {
    "bg": "#f6f0e4", "surface": "#fffdf8", "surface-2": "#efe7d6", "sidebar-bg": "#efe6d3",
    "topbar-bg": "#f6f0e4", "text": "#2b2118", "muted": "#6b5a47", "line": "#ddd0b8",
    "line-strong": "#b9a888", "hover": "#ede3cf", "accent": "#b4441f", "accent-hover": "#963716",
    "accent-ink": "#ffffff", "accent-text": "#963716", "accent-soft": "#f4dccf",
    "accent-soft-ink": "#7a2c10", "ok-bg": "#e1ecd2", "ok-ink": "#2f5a14", "warn-bg": "#f6e3b4",
    "warn-ink": "#6b4300", "danger": "#a82020", "danger-on": "#ffffff", "danger-bg": "#f8dcd6",
    "danger-ink": "#8a1a1a", "focus": "#1d4ed8", "avatar-bg": "#e3c4a8", "avatar-ink": "#6a2a10",
    "shadow": "none",
}  # fmt: skip
PAPER_DARK = {
    "bg": "#1d1812", "surface": "#26201a", "surface-2": "#302820", "sidebar-bg": "#221c16",
    "topbar-bg": "#1d1812", "text": "#f1e8d8", "muted": "#b7a88f", "line": "#3b3227",
    "line-strong": "#54483a", "hover": "#2d251d", "accent": "#e9895f", "accent-hover": "#f1a07d",
    "accent-ink": "#2a1206", "accent-text": "#f1a07d", "accent-soft": "#4a2a1b",
    "accent-soft-ink": "#f7d2bf", "ok-bg": "#20331a", "ok-ink": "#a6d98a", "warn-bg": "#3d2e10",
    "warn-ink": "#f0cb7a", "danger": "#f19a8e", "danger-on": "#330c08", "danger-bg": "#46201b",
    "danger-ink": "#ffc0b6", "focus": "#8ab4f8", "avatar-bg": "#6a3b24", "avatar-ink": "#f7d9c6",
    "shadow": "none",
}  # fmt: skip
MIDNIGHT_DARK = {
    "bg": "#0b0f14", "surface": "#121923", "surface-2": "#1a2330", "sidebar-bg": "#0e141c",
    "topbar-bg": "rgba(14,20,28,.9)", "text": "#e8eef6", "muted": "#8fa0b4", "line": "#1f2a38",
    "line-strong": "#34455a", "hover": "#172131", "accent": "#22d3ee", "accent-hover": "#67e8f9",
    "accent-ink": "#04222a", "accent-text": "#67e8f9", "accent-soft": "#123743",
    "accent-soft-ink": "#a5f3fc", "ok-bg": "#0f3a2a", "ok-ink": "#6ee7b7", "warn-bg": "#3b2a08",
    "warn-ink": "#fcd34d", "danger": "#fb7185", "danger-on": "#2a0710", "danger-bg": "#3c1320",
    "danger-ink": "#fda4af", "focus": "#7dd3fc", "avatar-bg": "#164e63", "avatar-ink": "#cffafe",
    "shadow": "0 0 0 1px rgba(34,211,238,.04)",
}  # fmt: skip
MIDNIGHT_LIGHT = {
    **NEUTRAL_LIGHT, "bg": "#eef3f8", "sidebar-bg": "#e3eaf2", "accent": "#0e7490",
    "accent-hover": "#155e75", "accent-text": "#155e75", "accent-soft": "#cdeff7",
    "accent-soft-ink": "#0b4a5c", "avatar-bg": "#b6e3ee", "avatar-ink": "#0b4a5c",
}  # fmt: skip

STUDIO = Theme(
    "studio", "Studio", "drawer", STUDIO_LIGHT, STUDIO_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "16px", "radius": "10px", "radius-sm": "10px",
        "radius-lg": "14px", "nav-pad": ".6rem .8rem", "cell-pad": ".7rem .75rem",
        "h1": "1.9rem", "sidebar-w": "16.5rem", "drawer-w": "26rem",
    },
    extra_css=".hn-avatar{width:2rem;height:2rem;font-size:.75rem}\n.hn-nav-item{font-weight:500}\n",
)  # fmt: skip
PAPER = Theme(
    "paper", "Paper", "page", PAPER_LIGHT, PAPER_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "16px", "radius": "2px", "radius-sm": "2px",
        "radius-lg": "2px", "nav-pad": ".5rem .9rem", "cell-pad": ".75rem .9rem",
        "h1": "2.1rem", "sidebar-w": "15rem", "drawer-w": "26rem",
    },
    extra_css=(
        'h1,h2,h3,.hn-brand{font-family:Georgia,"Iowan Old Style","Times New Roman",serif;'
        "font-weight:600;letter-spacing:-.01em}\n"
        ".hn-card{box-shadow:none;border-width:1px 0}\n"
        ".hn-table th{text-transform:uppercase;font-size:.72rem;letter-spacing:.08em;"
        "background:transparent;border-bottom:2px solid var(--hn-line-strong)}\n"
    ),
)  # fmt: skip
MIDNIGHT = Theme(
    "midnight", "Midnight", "drawer", MIDNIGHT_LIGHT, MIDNIGHT_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "15px", "radius": "6px", "radius-sm": "4px",
        "radius-lg": "8px", "nav-pad": ".55rem .8rem", "cell-pad": ".65rem .75rem",
        "h1": "1.7rem", "sidebar-w": "15.5rem", "drawer-w": "25rem",
    },
    default_mode="dark",
    extra_css=(
        ".hn-primary{box-shadow:0 0 18px rgba(34,211,238,.25)}\n"
        'h1,h2,h3,.hn-brand,.hn-table th,.hn-nav-item{font-family:ui-monospace,"JetBrains Mono",'
        "Menlo,Consolas,monospace}\n"
        ".hn-table th{text-transform:uppercase;font-size:.7rem;letter-spacing:.1em}\n"
        ".hn-nav-item[aria-current=page]{box-shadow:inset 2px 0 var(--hn-accent)}\n"
    ),
)  # fmt: skip

THEMES: dict[str, Theme] = {t.name: t for t in (STUDIO, MIDNIGHT, PAPER)}
for built_in in THEMES.values():
    built_in.check()


def resolve(theme: str | Theme) -> Theme:
    """A built-in theme by name, or a custom ``Theme`` (checked)."""
    if isinstance(theme, Theme):
        theme.check()
        return theme
    try:
        return THEMES[theme]
    except KeyError:
        raise ValueError(
            f"unknown theme {theme!r}; built in: {sorted(THEMES)}"
        ) from None
