"""Admin themes: a theme is a set of design tokens plus two layout choices.

Every component template and ``ui/base.css`` use only ``--hn-*`` custom
properties, so a theme restyles the whole admin by defining the tokens below
for a light and a dark scheme (plus optional extra CSS). ``edit`` chooses where
the edit form opens: ``"drawer"`` (beside the list) or ``"page"``.
A theme whose ``default_mode`` is ``"dark"`` is dark unless the user switches.

Built in: ``studio`` (default), ``classic``, ``compact``, ``midnight``.
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
CLASSIC_LIGHT = {
    "bg": "#f4f5f7", "surface": "#ffffff", "surface-2": "#eceef1", "sidebar-bg": "#ffffff",
    "topbar-bg": "#ffffff", "text": "#1c2430", "muted": "#566070", "line": "#dde1e6",
    "line-strong": "#b8c0ca", "hover": "#f0f2f5", "accent": "#1f5fa8", "accent-hover": "#174a85",
    "accent-ink": "#ffffff", "accent-text": "#174a85", "accent-soft": "#dbe8f7",
    "accent-soft-ink": "#123e72", "ok-bg": "#dff3e4", "ok-ink": "#18633a", "warn-bg": "#fbebd0",
    "warn-ink": "#7a4a05", "danger": "#b42318", "danger-on": "#ffffff", "danger-bg": "#fde7e5",
    "danger-ink": "#912018", "focus": "#1a73e8", "avatar-bg": "#c5d6ea", "avatar-ink": "#17406e",
    "shadow": "none",
}  # fmt: skip
CLASSIC_DARK = {
    "bg": "#12161c", "surface": "#1a2029", "surface-2": "#222a35", "sidebar-bg": "#1a2029",
    "topbar-bg": "#1a2029", "text": "#e6eaf0", "muted": "#9aa5b4", "line": "#2c3542",
    "line-strong": "#425063", "hover": "#202833", "accent": "#6aa7ea", "accent-hover": "#8fbdf2",
    "accent-ink": "#08203a", "accent-text": "#8fbdf2", "accent-soft": "#1f3552",
    "accent-soft-ink": "#cfe3fb", "ok-bg": "#153a26", "ok-ink": "#86e0a8", "warn-bg": "#3a2b0c",
    "warn-ink": "#f2c46e", "danger": "#f28b82", "danger-on": "#3a0d0a", "danger-bg": "#411a17",
    "danger-ink": "#ffb4ac", "focus": "#8ab4f8", "avatar-bg": "#2f4f7a", "avatar-ink": "#d6e6fb",
    "shadow": "none",
}  # fmt: skip
COMPACT_LIGHT = {
    **CLASSIC_LIGHT, "bg": "#f2f5f5", "surface-2": "#e8eeee", "line": "#d8e0e0",
    "line-strong": "#b4c2c2", "hover": "#eaf1f1", "accent": "#0f766e", "accent-hover": "#0b5f58",
    "accent-text": "#0b5f58", "accent-soft": "#d3ece9", "accent-soft-ink": "#0b4f49",
    "avatar-bg": "#b8dedb", "avatar-ink": "#0b4f49",
}  # fmt: skip
COMPACT_DARK = {
    **CLASSIC_DARK, "bg": "#0f1616", "surface": "#162020", "surface-2": "#1d2a2a",
    "sidebar-bg": "#162020", "topbar-bg": "#162020", "line": "#263636", "line-strong": "#3a5252",
    "hover": "#1b2929", "accent": "#4fd1c5", "accent-hover": "#7ee3d9", "accent-ink": "#06201d",
    "accent-text": "#7ee3d9", "accent-soft": "#17403c", "accent-soft-ink": "#c4f1ec",
    "avatar-bg": "#1f5a54", "avatar-ink": "#d3f5f1",
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
    **CLASSIC_LIGHT, "bg": "#eef3f8", "sidebar-bg": "#0e141c", "accent": "#0e7490",
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
CLASSIC = Theme(
    "classic", "Classic", "page", CLASSIC_LIGHT, CLASSIC_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "16px", "radius": "4px", "radius-sm": "3px",
        "radius-lg": "4px", "nav-pad": ".5rem .75rem", "cell-pad": ".6rem .75rem",
        "h1": "1.5rem", "sidebar-w": "15rem", "drawer-w": "26rem",
    },
    extra_css=".hn-card{box-shadow:none}\n.hn-table th{background:var(--hn-surface-2)}\n",
)  # fmt: skip
COMPACT = Theme(
    "compact", "Compact", "page", COMPACT_LIGHT, COMPACT_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "14px", "radius": "3px", "radius-sm": "2px",
        "radius-lg": "3px", "nav-pad": ".35rem .6rem", "cell-pad": ".3rem .6rem",
        "h1": "1.3rem", "sidebar-w": "12.5rem", "drawer-w": "22rem",
    },
    extra_css=(
        ".hn-table tbody tr:nth-child(even){background:var(--hn-surface-2)}\n"
        ".hn-main{padding:1rem;gap:.9rem}\n.hn-btn{min-height:2rem;padding:.25rem .7rem}\n"
        ".hn-iconbtn{width:2rem;height:2rem}\n.hn-stat-value{font-size:1.5rem}\n"
    ),
)  # fmt: skip
MIDNIGHT = Theme(
    "midnight", "Midnight", "drawer", MIDNIGHT_LIGHT, MIDNIGHT_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "16px", "radius": "10px", "radius-sm": "8px",
        "radius-lg": "14px", "nav-pad": ".55rem .8rem", "cell-pad": ".65rem .75rem",
        "h1": "1.7rem", "sidebar-w": "15.5rem", "drawer-w": "25rem",
    },
    default_mode="dark",
    extra_css=".hn-primary{box-shadow:0 0 18px rgba(34,211,238,.25)}\n",
)  # fmt: skip

THEMES: dict[str, Theme] = {t.name: t for t in (STUDIO, CLASSIC, COMPACT, MIDNIGHT)}
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
