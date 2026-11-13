"""Builders for the admin's UI fragments (badges, tables, pagination, ...).

Each function renders one of the base components in ``ui/components`` and
returns ``Raw`` HTML. Everything that comes from application data goes through
the template engine's escaping; icon names and tones are checked against fixed
lists so they can never carry markup.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from typing import Any

from hinoki.admin.parts import TONES, Avatar, Badge, Stat
from hinoki.admin.templates import UI, Raw, Templates, escape

ICONS = frozenset(re.findall(r'id="i-([a-z0-9-]+)"', (UI / "icons.svg").read_text()))
CELL_LIMIT = 80
WINDOW = 2


def icon_name(name: str, fallback: str = "file-text") -> str:
    return name if name in ICONS else fallback


def tone_name(tone: str) -> str:
    return tone if tone in TONES else "neutral"


def initials(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    return "".join(w[0] for w in words[:2]).upper() or "?"


class Components:
    """UI fragments for one admin (they share its templates)."""

    def __init__(self, templates: Templates) -> None:
        self.t = templates

    def badge(self, badge: Badge) -> Raw:
        return self.t.render(
            "badge",
            label=badge.label,
            tone=tone_name(badge.tone),
            icon=icon_name(badge.icon, "circle-check"),
        )

    def avatar(self, name: str, *, show_name: bool = True) -> Raw:
        label = Raw(f"<span>{escape(name)}</span>") if show_name else Raw("")
        return self.t.render("avatar", initials=initials(name), name=label)

    def notice(self, kind: str, message: str) -> Raw:
        tone = {"error": "danger", "ok": "ok"}.get(kind, "accent")
        icon = {"error": "circle-alert", "ok": "circle-check"}.get(kind, "info")
        role = "alert" if kind == "error" else "status"
        return self.t.render("notice", tone=tone, icon=icon, role=role, message=message)

    def cell(self, value: Any) -> Raw:
        """A table cell's content for a value."""
        if isinstance(value, Badge):
            return self.badge(value)
        if isinstance(value, Avatar):
            return self.avatar(value.name)
        if isinstance(value, Raw):
            return value
        return escape(plain_text(value))

    def nav_item(
        self, href: str, label: str, icon: str, count: int | None, current: bool
    ) -> Raw:
        badge = (
            Raw(f'<span class="hn-count">{count}</span>')
            if count is not None
            else Raw("")
        )
        mark = Raw(' aria-current="page"') if current else Raw("")
        return self.t.render(
            "navitem",
            href=href,
            label=label,
            icon=icon_name(icon),
            count=badge,
            current=mark,
        )

    def stat(self, stat: Stat) -> Raw:
        return self.t.render(
            "statcard",
            label=stat.label,
            value=stat.value,
            icon=icon_name(stat.icon),
            tone=tone_name(stat.tone),
            spark=sparkline(stat.series),
        )

    def panel(self, ident: str, title: str, body: Raw, tools: Raw = Raw("")) -> Raw:  # noqa: B008
        return self.t.render("card", ident=ident, title=title, tools=tools, body=body)

    def pagination(self, urls: Sequence[tuple[str, str, bool]], summary: str) -> Raw:
        """``urls`` are ``(href, label, current)``; an empty href is a disabled link."""
        links = []
        for href, label, current in urls:
            mark = ' aria-current="page"' if current else ""
            if href:
                links.append(
                    f'<a class="hn-page" href="{escape(href)}"{mark}>{label}</a>'
                )
            else:
                links.append(
                    f'<span class="hn-page" aria-disabled="true">{label}</span>'
                )
        return self.t.render("pagination", summary=summary, pages=Raw("".join(links)))


def plain_text(value: Any) -> str:
    """Display text for a value (long text is cut)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M")
    text = str(value)
    return text if len(text) <= CELL_LIMIT else text[: CELL_LIMIT - 1] + "…"


def sparkline(series: Sequence[float]) -> Raw:
    """A small line chart for numbers (oldest first); nothing for fewer than two."""
    points = [float(v) for v in series][-40:]
    if len(points) < 2:  # noqa: PLR2004 - a line needs two points
        return Raw("")
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    step = 112 / (len(points) - 1)
    coords = [(i * step, 36 - (v - low) / span * 32) for i, v in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,40 {line} 112,40"
    return Raw(
        '<svg class="hn-spark" viewBox="0 0 112 40" role="img" aria-label="Trend" '
        'preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="currentColor" fill-opacity=".12"/>'
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round" '
        'vector-effect="non-scaling-stroke"/></svg>'
    )


def page_window(page: int, pages: int) -> list[int | None]:
    """Page numbers to show: first, last, the neighbours of ``page``, with ``None``
    standing for a gap."""
    wanted = {1, pages, *range(max(1, page - WINDOW), min(pages, page + WINDOW) + 1)}
    shown: list[int | None] = []
    last = 0
    for number in sorted(wanted):
        if number - last > 1:
            shown.append(None)
        shown.append(number)
        last = number
    return shown
