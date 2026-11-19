"""What Kiso says on stderr when it starts: its name, version and how it runs.

The ``listening on`` line stays on stdout, where tools wait for it; this is the
branded line before it. It is coloured only when stderr is a terminal that takes
colour (and ``NO_COLOR`` is unset), and is one plain line otherwise.
"""

from __future__ import annotations

import os
import sys

from hinoki.settings import ServerSettings

TAGLINE = "Hinoki's Rust server"
BOLD_CYAN, DIM, RESET = "\x1b[1;36m", "\x1b[2m", "\x1b[0m"


def wants_colour() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def details(
    settings: ServerSettings, *, dev: bool, workers: int, scheme: str
) -> list[str]:
    """The facts worth knowing at a glance, in the order they are shown."""
    return [
        "dev, reloads on file changes" if dev else "serve",
        f"{workers} worker{'s' if workers != 1 else ''}",
        scheme,
        f"access log {'on' if settings.logs_access(dev) else 'off'}",
        f"native lane {'off' if settings.native_dispatch is False else 'on'}",
    ]


def banner(
    settings: ServerSettings,
    *,
    version: str,
    dev: bool,
    workers: int,
    scheme: str,
    colour: bool,
) -> str:
    facts = details(settings, dev=dev, workers=workers, scheme=scheme)
    if not colour:
        return f"Kiso {version} ({TAGLINE}): {', '.join(facts)}"
    name = f"{BOLD_CYAN}Kiso {version}{RESET}"
    return f"{name}  {DIM}{TAGLINE}{RESET}\n  {DIM}{' \u00b7 '.join(facts)}{RESET}"


def show(
    settings: ServerSettings, *, version: str, dev: bool, workers: int, scheme: str
) -> None:
    text = banner(
        settings,
        version=version,
        dev=dev,
        workers=workers,
        scheme=scheme,
        colour=wants_colour(),
    )
    print(text, file=sys.stderr, flush=True)
