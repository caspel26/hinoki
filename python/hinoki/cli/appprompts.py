"""Questions and report for ``hinoki app create`` in a terminal."""

import shutil
import sys
from argparse import Namespace
from collections.abc import Collection
from pathlib import Path

from hinoki.cli.new import (
    Ask,
    LinePrompts,
    Questions,
    cancelled,
    show_banner,
)
from hinoki.cli.prompts import Check, Prompter, keys_available, raw_keys
from hinoki.project import ProjectManifest
from hinoki.scaffold import NAME, RESERVED_NAMES
from hinoki.terminal import Style, shown_path

APP_FILES = (
    ("models.py", "tables of the app"),
    ("api.py", "endpoints of the app"),
    ("routes.py", "routes served, mounted at the prefix"),
    ("migrations/", "migration history"),
)


def name_check(taken: Collection[str]) -> Check:
    def check(name: str) -> str | None:
        if not name:
            return "an app name is required"
        if name in RESERVED_NAMES or not NAME.fullmatch(name):
            return "use lower-case letters, digits and single underscores from a letter"
        if name in taken:
            return f"app '{name}' is already registered"
        return None

    return check


def prefix_check(prefix: str) -> str | None:
    if not prefix.startswith("/") or prefix.endswith("/"):
        return "start with '/' and do not end with one"
    return None


def requires_check(known: Collection[str]) -> Check:
    def check(text: str) -> str | None:
        unknown = [name for name in split_requires(text) if name not in known]
        return f"unknown app: {', '.join(unknown)}" if unknown else None

    return check


def split_requires(text: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in text.split(",") if item.strip())


def fill(args: Namespace, manifest: ProjectManifest, questions: Questions) -> None:
    """Ask only for what the flags left unset."""
    known = [app.name for app in manifest.apps]
    if args.name is None:
        args.name = questions.text(
            "App name",
            "Name",
            "",
            name_check(known),
            "lower-case letters and digits",
        )
    if args.prefix is None:
        args.prefix = questions.text(
            "URL prefix", "Prefix", f"/{args.name}", prefix_check
        )
    if args.requires is None:
        args.requires = (
            questions.text(
                "Depends on",
                "Requires",
                "",
                requires_check(known),
                f"comma-separated, from {', '.join(known)}",
            )
            if known
            else ""
        )


def ask_app(args: Namespace, manifest: ProjectManifest, ask: Ask, style: Style) -> int:
    """Run the questions; 130 when the user cancels."""
    show_banner(style, "new app")
    try:
        if style.fancy and keys_available():
            width = shutil.get_terminal_size().columns
            with raw_keys(sys.stdout) as keys:
                fill(args, manifest, Prompter(style, keys, sys.stdout, width))
        else:
            fill(args, manifest, LinePrompts(ask, style))
    except (EOFError, KeyboardInterrupt):
        print(cancelled(style), file=sys.stderr)
        return 130
    print(style.line("rail") if style.fancy else "")
    return 0


def report_app(
    style: Style,
    name: str,
    created: Path,
    manifest: Path,
    prefix: str,
    requires: tuple[str, ...],
    next_steps: bool = True,
) -> list[str]:
    """The lines shown once the app exists.

    ``hinoki new`` passes ``next_steps=False`` and shows its own."""
    if not style.fancy:
        return [
            f"created app {name} at {shown_path(created)}",
            f"registered in {shown_path(manifest)}",
        ]
    mounted = f"mounted at {prefix or f'/{name}'}"
    if requires:
        mounted += f", requires {', '.join(requires)}"
    lines = [
        style.ok(f"Created app {style.bold(name)}  {style.dim(mounted)}"),
        style.line("rail", style.paint(f"{shown_path(created)}/", "light", bold=True)),
    ]
    for index, (path, what) in enumerate(APP_FILES):
        branch = "\u2514\u2500" if index == len(APP_FILES) - 1 else "\u251c\u2500"
        entry = f"{style.dim(branch)} {style.text(f'{path:<14}')}{style.dim(what)}"
        lines.append(style.line("rail", entry))
    registered = style.dim(f"registered app {name}")
    lines += [
        style.line("rail"),
        style.line(
            "rail",
            f"{style.paint('+', 'good')} {style.text('hinoki.toml')}  {registered}",
        ),
        style.line("rail"),
    ]
    if not next_steps:
        return lines
    lines += [
        style.line("active", style.bold("Next steps")),
        style.line(
            "rail", f"{style.paint('$', 'brand')} uv run hinoki migrations plan"
        ),
        style.line("end", style.dim("after adding models to models.py")),
        "",
    ]
    return lines
