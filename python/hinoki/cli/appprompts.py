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
    ("routes.py", "reusable routes of the app"),
    ("migrations/", "migration history"),
)
APP_API_ONLY_FILES = (
    ("api.py", "endpoints of the app"),
    ("routes.py", "reusable routes of the app"),
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
    requires: tuple[str, ...],
    *,
    no_models: bool = False,
    next_steps: bool = True,
) -> list[str]:
    """The lines shown once the app exists.

    ``hinoki new`` passes ``next_steps=False`` and shows its own."""
    package = created.parent.parent.name if created.parent.name == "apps" else ""
    routes_file = f"{package}/routes.py" if package else "routes.py"
    import_line = f"from .apps.{name}.routes import routes as {name}_routes"
    mount_line = f'Mount("/{name}", {name}_routes)'
    if not style.fancy:
        return [
            f"created app {name} at {shown_path(created)}",
            f"registered in {shown_path(manifest)}",
            f"expose in {routes_file}: {import_line}; {mount_line}",
        ]
    summary_parts = ["API-only"] if no_models else []
    if requires:
        summary_parts.append(f"requires {', '.join(requires)}")
    detail = ", ".join(summary_parts)
    heading = f"Created app {style.bold(name)}" + (
        f"  {style.dim(detail)}" if detail else ""
    )
    files = APP_API_ONLY_FILES if no_models else APP_FILES
    lines = [
        style.ok(heading),
        style.line("rail", style.paint(f"{shown_path(created)}/", "light", bold=True)),
    ]
    for index, (path, what) in enumerate(files):
        branch = "\u2514\u2500" if index == len(files) - 1 else "\u251c\u2500"
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
        style.line("rail", style.dim(f"to expose in {routes_file}:")),
        style.line("rail", f"  {style.text(import_line)}"),
        style.line("rail", f"  {style.text(mount_line)}"),
    ]
    if no_models:
        lines.append(
            style.line("end", style.dim("API-only app (no models/migrations)"))
        )
    else:
        lines += [
            style.line(
                "rail", f"{style.paint('$', 'brand')} uv run hinoki migrations plan"
            ),
            style.line("end", style.dim("after adding models to models.py")),
        ]
    lines.append("")
    return lines
