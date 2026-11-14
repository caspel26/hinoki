"""Admin themes: a theme is a set of design tokens plus two layout choices.

Every component template and ``ui/base.css`` use only ``--hn-*`` custom
properties, so a theme restyles the whole admin by defining the tokens below
for a light and a dark scheme (plus optional extra CSS). ``edit`` chooses where
the edit form opens: ``"drawer"`` (beside the list) or ``"page"``.
A theme whose ``default_mode`` is ``"dark"`` is dark unless the user switches.

Built in, four deliberately different *styles*, each with a complete light and
dark scheme (the user's system preference decides, and the toggle overrides it):

* ``studio`` (default): sidebar navigation, cards, soft violet, rounded, the
  edit form opens in a drawer beside the list.
* ``console``: no sidebar, a horizontal navigation bar, a dense monospaced grid
  with sticky headers, square corners, neon-cyan accent (dark) / teal (light).
* ``paper``: a quiet text index on the left and one centred reading column, serif
  type, the list as a document index of rows, terracotta accent, edit on a page.
* ``grove``: a familiar sidebar, open row list, green accents and page-based
  editing, with a light lichen palette and a dark pine palette.

``layout`` (``"sidebar"``, ``"topbar"``, ``"index"``) and ``list_style``
(``"table"``, ``"rows"``) choose the structure; the tokens choose the look.
To make your own, build a ``Theme`` and pass it as ``Admin(theme=...)``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

LAYOUTS = {"sidebar", "topbar", "index"}
LIST_STYLES = {"table", "rows"}
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
SERIF_FONT = 'Charter, "Iowan Old Style", Georgia, "Times New Roman", serif'
MONO_FONT = 'ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'
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
    layout: str = "sidebar"  # "sidebar", "topbar" or "index" (see base.css)
    list_style: str = "table"  # "table" or "rows"

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
        if self.layout not in LAYOUTS or self.list_style not in LIST_STYLES:
            raise ValueError(f"theme {self.name!r}: bad layout or list_style")
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
CONSOLE_DARK = {
    "bg": "#0b0f14", "surface": "#121923", "surface-2": "#1a2330", "sidebar-bg": "#0e141c",
    "topbar-bg": "rgba(14,20,28,.9)", "text": "#e8eef6", "muted": "#8fa0b4", "line": "#1f2a38",
    "line-strong": "#34455a", "hover": "#172131", "accent": "#22d3ee", "accent-hover": "#67e8f9",
    "accent-ink": "#04222a", "accent-text": "#67e8f9", "accent-soft": "#123743",
    "accent-soft-ink": "#a5f3fc", "ok-bg": "#0f3a2a", "ok-ink": "#6ee7b7", "warn-bg": "#3b2a08",
    "warn-ink": "#fcd34d", "danger": "#fb7185", "danger-on": "#2a0710", "danger-bg": "#3c1320",
    "danger-ink": "#fda4af", "focus": "#7dd3fc", "avatar-bg": "#164e63", "avatar-ink": "#cffafe",
    "shadow": "0 0 0 1px rgba(34,211,238,.04)",
}  # fmt: skip
CONSOLE_LIGHT = {
    **NEUTRAL_LIGHT, "bg": "#eef3f8", "sidebar-bg": "#e3eaf2", "accent": "#0e7490",
    "accent-hover": "#155e75", "accent-text": "#155e75", "accent-soft": "#cdeff7",
    "accent-soft-ink": "#0b4a5c", "avatar-bg": "#b6e3ee", "avatar-ink": "#0b4a5c",
}  # fmt: skip
GROVE_LIGHT = {
    "bg": "#f2f5ef", "surface": "#fbfcf9", "surface-2": "#e6ede4",
    "sidebar-bg": "#e9f0e6", "topbar-bg": "#fbfcf9", "text": "#1f2d24",
    "muted": "#536456", "line": "#d7e0d4", "line-strong": "#aab9a8",
    "hover": "#eaf1e8", "accent": "#2f6b4e", "accent-hover": "#25583e",
    "accent-ink": "#ffffff", "accent-text": "#225b3d", "accent-soft": "#dcebdd",
    "accent-soft-ink": "#1e5034", "ok-bg": "#e2f1e5", "ok-ink": "#245e36",
    "warn-bg": "#f5eccf", "warn-ink": "#684400", "danger": "#b42318",
    "danger-on": "#ffffff", "danger-bg": "#fde7e5", "danger-ink": "#912018",
    "focus": "#1d4ed8", "avatar-bg": "#cfe1ce", "avatar-ink": "#294f32",
    "shadow": "0 1px 2px rgba(31,65,40,.04)",
}  # fmt: skip
GROVE_DARK = {
    "bg": "#111a14", "surface": "#19251c", "surface-2": "#243329",
    "sidebar-bg": "#141f17", "topbar-bg": "#19251c", "text": "#edf4ed",
    "muted": "#b6c7b8", "line": "#314435", "line-strong": "#4b6250",
    "hover": "#26362a", "accent": "#8fc99c", "accent-hover": "#a4d9af",
    "accent-ink": "#102016", "accent-text": "#9bd2a6", "accent-soft": "#263c2c",
    "accent-soft-ink": "#d1e8d4", "ok-bg": "#203b29", "ok-ink": "#9edaac",
    "warn-bg": "#403513", "warn-ink": "#f0d88c", "danger": "#ff8b8b",
    "danger-on": "#330b0b", "danger-bg": "#441c1b", "danger-ink": "#ffb8af",
    "focus": "#91c5ff", "avatar-bg": "#365a3d", "avatar-ink": "#d9edd9",
    "shadow": "none",
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
        "font": SERIF_FONT, "font-size": "17px", "radius": "2px", "radius-sm": "2px",
        "radius-lg": "2px", "nav-pad": ".4rem .5rem", "cell-pad": ".75rem .9rem",
        "h1": "2.3rem", "sidebar-w": "12.5rem", "drawer-w": "26rem",
    },
    layout="index", list_style="rows",
    extra_css=(
        'h1,h2,h3,.hn-brand{font-family:Georgia,"Iowan Old Style","Times New Roman",serif;'
        "font-weight:600;letter-spacing:-.01em}\n"
        ".hn-card{box-shadow:none;border-width:1px 0;background:transparent}\n"
        ".hn-pageheader{border-bottom:2px solid var(--hn-line-strong);padding-bottom:.9rem}\n"
        ".hn-btn{border-radius:0}\n"
    ),
)  # fmt: skip
CONSOLE = Theme(
    "console", "Console", "drawer", CONSOLE_LIGHT, CONSOLE_DARK,
    {
        "font": MONO_FONT, "font-size": "13.5px", "radius": "2px", "radius-sm": "2px",
        "radius-lg": "3px", "nav-pad": ".3rem .75rem", "cell-pad": ".3rem .65rem",
        "h1": "1.25rem", "sidebar-w": "13rem", "drawer-w": "24rem",
    },
    layout="topbar", list_style="table",
    extra_css=(
        ".hn-primary{box-shadow:0 0 14px color-mix(in srgb,var(--hn-accent) 35%,transparent)}\n"
        ".hn-table{border:1px solid var(--hn-line)}\n"
        ".hn-table th{position:sticky;top:0;text-transform:uppercase;font-size:.7rem;"
        "letter-spacing:.1em;background:var(--hn-surface-2);border-bottom:1px solid var(--hn-line-strong)}\n"
        ".hn-table td,.hn-table th{border-right:1px solid var(--hn-line)}\n"
        ".hn-table tbody tr:nth-child(even){background:var(--hn-surface-2)}\n"
        ".hn-nav-item[aria-current=page]{background:none;box-shadow:inset 0 -2px var(--hn-accent)}\n"
        ".hn-card{box-shadow:none}\n.hn-stat-value{font-size:1.5rem}\n"
    ),
)  # fmt: skip
GROVE = Theme(
    "grove", "Grove", "page", GROVE_LIGHT, GROVE_DARK,
    {
        "font": SYSTEM_FONT, "font-size": "15px", "radius": "8px", "radius-sm": "5px",
        "radius-lg": "12px", "nav-pad": ".55rem .75rem", "cell-pad": ".7rem .75rem",
        "h1": "1.85rem", "sidebar-w": "15rem", "drawer-w": "26rem",
    },
    layout="sidebar", list_style="rows",
    extra_css=(
        ".hn-card{box-shadow:none}\n"
        ".hn-nav-item[aria-current=page]{border-left:3px solid var(--hn-accent);"
        "border-radius:0 var(--hn-radius-sm) var(--hn-radius-sm) 0}\n"
        ".hn-pageheader{border-bottom:1px solid var(--hn-line-strong);padding-bottom:1rem}\n"
        ".hn-list-rows .hn-table tr{padding:.85rem .4rem}\n"
        ".hn-list-rows .hn-table td:not(.hn-check){min-width:0;white-space:normal;"
        "overflow-wrap:anywhere}\n"
        "@media(max-width:42rem){"
        ".hn-list-rows .hn-table tr{display:grid;"
        "grid-template-columns:1.5rem minmax(0,1fr);align-items:start;gap:.35rem .65rem}\n"
        ".hn-list-rows .hn-table td{grid-column:2}\n"
        ".hn-list-rows .hn-table td.hn-check{grid-column:1;grid-row:1;width:auto;align-self:start}\n"
        ".hn-list-rows .hn-table td:has(.hn-rowlink){grid-row:1;font-size:1.05em}\n"
        "}\n"
    ),
)  # fmt: skip

THEMES: dict[str, Theme] = {t.name: t for t in (STUDIO, CONSOLE, PAPER, GROVE)}
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
