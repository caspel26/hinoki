"""What Kiso says on stderr when it starts: its name, version and how it runs.

The ``listening on`` line stays on stdout, where tools wait for it; this is the
box before it, the one ``hinoki new`` draws, titled ``Kiso``: the tagline and the
version, then the facts about how the server runs. It is drawn when stderr is a
terminal that takes colour (and ``NO_COLOR`` is unset); otherwise it is one plain
line.
"""

from __future__ import annotations

import os
import shutil
import sys

from hinoki.settings import ServerSettings
from hinoki.terminal import Style, color_depth, light_background

TAGLINE = "Hinoki's Rust server"
SEPARATOR = " \u00b7 "


def wants_colour() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def brand_style() -> Style:
    """The palette of ``hinoki new``, at the depth and background of this terminal."""
    return Style(
        color=True,
        unicode=True,
        depth=color_depth(os.environ),
        light=light_background(os.environ),
        width=shutil.get_terminal_size().columns,
    )


def wrapped(facts: list[str], room: int) -> list[str]:
    """``facts`` joined with dots, broken into rows of at most ``room`` columns."""
    rows = [facts[0]]
    for fact in facts[1:]:
        if len(rows[-1]) + len(SEPARATOR) + len(fact) <= room:
            rows[-1] += SEPARATOR + fact
        else:
            rows.append(fact)
    return rows


def details(
    settings: ServerSettings,
    *,
    dev: bool,
    workers: int,
    scheme: str,
    interface: str = "asgi",
) -> list[str]:
    """The facts worth knowing at a glance, in the order they are shown."""
    lane = (
        "WSGI, Python threads"
        if interface == "wsgi"
        else f"native lane {'off' if settings.native_dispatch is False else 'on'}"
    )
    return [
        "dev, reloads on file changes" if dev else "serve",
        f"{workers} worker{'s' if workers != 1 else ''}",
        scheme,
        f"access log {'on' if settings.logs_access(dev) else 'off'}",
        lane,
    ]


def banner(
    settings: ServerSettings,
    *,
    version: str,
    dev: bool,
    workers: int,
    scheme: str,
    style: Style | None,
    interface: str = "asgi",
) -> str:
    """The banner for ``style``: plain on one line when it is ``None``."""
    facts = details(
        settings, dev=dev, workers=workers, scheme=scheme, interface=interface
    )
    if style is None:
        return f"Kiso {version} ({TAGLINE}): {', '.join(facts)}"
    rows = [(style.paint(TAGLINE, "brand"), style.dim(version)), ("", "")]
    rows += [(style.dim(row), "") for row in wrapped(facts, style.box_room())]
    return style.framed("Kiso", rows)


def show(
    settings: ServerSettings,
    *,
    version: str,
    dev: bool,
    workers: int,
    scheme: str,
    interface: str = "asgi",
) -> None:
    style = brand_style() if wants_colour() else None
    text = banner(
        settings,
        version=version,
        dev=dev,
        workers=workers,
        scheme=scheme,
        style=style,
        interface=interface,
    )
    print(text, file=sys.stderr, flush=True)
