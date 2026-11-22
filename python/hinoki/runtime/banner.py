"""What Kiso says on stderr when it starts: its name, version and how it runs.

The ``listening on`` line stays on stdout, where tools wait for it; this is the
branch before it, the one ``hinoki new`` draws, titled ``Kiso``: the tagline and
the version, then the facts about how the server runs. It is drawn when stderr is a
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


def facts(
    settings: ServerSettings,
    *,
    dev: bool,
    workers: int,
    scheme: str,
    interface: str = "asgi",
) -> list[tuple[str, str]]:
    """The facts worth knowing at a glance, as key and value, in the order
    they are shown."""
    lane = (
        ("interface", "WSGI, Python threads")
        if interface == "wsgi"
        else ("native lane", "off" if settings.native_dispatch is False else "on")
    )
    return [
        ("mode", "dev, reloads on file changes" if dev else "serve"),
        ("workers", str(workers)),
        ("scheme", scheme),
        ("access log", "on" if settings.logs_access(dev) else "off"),
        lane,
    ]


def plain_fact(key: str, value: str) -> str:
    """One fact as the plain line says it."""
    if key == "workers":
        return f"{value} worker{'s' if value != '1' else ''}"
    return f"{key} {value}" if key in {"access log", "native lane"} else value


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
    shown = facts(
        settings, dev=dev, workers=workers, scheme=scheme, interface=interface
    )
    if style is None:
        plain = ", ".join(plain_fact(key, value) for key, value in shown)
        return f"Kiso {version} ({TAGLINE}): {plain}"
    return style.lockup("Kiso", TAGLINE, f"v{version}", shown)


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
