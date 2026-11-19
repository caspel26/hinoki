"""``hinoki new``: create a project, interactively when run from a terminal."""

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from hinoki.scaffold import DATABASES, NAME, RESERVED_NAMES, ScaffoldError, create
from hinoki.terminal import Style

Ask = Callable[[str], str]


def add_arguments(new: argparse.ArgumentParser) -> None:
    new.add_argument(
        "name",
        nargs="?",
        help="package name: lower-case letters and digits (asked for in a terminal)",
    )
    new.add_argument("--dir", default=None, help="parent directory (default: here)")
    new.add_argument(
        "--hinoki",
        default=None,
        help="where uv finds Hinoki: a source checkout path or a git URL[@rev]",
    )
    new.add_argument(
        "--database",
        choices=DATABASES,
        default=None,
        help="declare a database setting (default: none)",
    )
    new.add_argument(
        "--agent-files",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="write AGENTS.md, CLAUDE.md and .mcp.json (default: yes)",
    )
    new.add_argument(
        "--git",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run `git init` and make no commit (default: no)",
    )
    new.add_argument(
        "--install",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run `uv sync` in the new project (default: no)",
    )
    new.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt; use the flags and defaults",
    )


def ask_text(ask: Ask, style: Style, question: str, default: str) -> str:
    return ask(style.question(question, default)).strip() or default


CHOICE_HINTS = {
    "none": "no database setting",
    "sqlite": "local file in .hinoki/",
    "postgresql": "also writes .env.example",
}


def ask_choice(ask: Ask, style: Style, question: str, default: str) -> str:
    """Numbered choices; the answer is a number or the name."""
    print(style.heading(question))
    for index, name in enumerate(DATABASES, start=1):
        print(style.option(index, name, CHOICE_HINTS[name], default=name == default))
    while True:
        answer = ask_text(ask, style, "Choose", default)
        if answer in DATABASES:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(DATABASES):
            return DATABASES[int(answer) - 1]
        print(style.warning(f"choose one of: {', '.join(DATABASES)}"), file=sys.stderr)


def ask_yes(ask: Ask, style: Style, question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(style.question(question, hint)).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print(style.warning("answer y or n"), file=sys.stderr)


def ask_name(ask: Ask, style: Style) -> str:
    while True:
        name = ask_text(ask, style, "Project name (lower-case letters and digits)", "")
        if NAME.fullmatch(name) and name not in RESERVED_NAMES:
            return name
        print(
            style.warning(
                "use lower-case letters and digits, starting with a letter; "
                "not 'hinoki' or a standard-library module name"
            ),
            file=sys.stderr,
        )


def interactive(args: argparse.Namespace) -> bool:
    return not args.no_input and sys.stdin.isatty() and sys.stdout.isatty()


def fill_from_prompts(
    args: argparse.Namespace, ask: Ask, style: Style | None = None
) -> None:
    """Ask only for what the flags left unset."""
    style = style or Style()
    if args.name is None:
        args.name = ask_name(ask, style)
    if args.dir is None:
        args.dir = ask_text(ask, style, "Parent directory", ".")
    if args.database is None:
        args.database = ask_choice(ask, style, "Database", "none")
    if args.agent_files is None:
        args.agent_files = ask_yes(
            ask, style, "Agent instructions and MCP config?", True
        )
    if args.git is None:
        args.git = ask_yes(ask, style, "Initialize a git repository?", True)
    if args.install is None:
        args.install = ask_yes(ask, style, "Install dependencies (uv sync)?", True)


def run_tool(command: list[str], cwd: Path, style: Style) -> bool:
    """Run one setup command; a missing tool or a failure is reported, not fatal."""
    label = " ".join(command)
    tool = shutil.which(command[0])
    if tool is None:
        print(style.skipped(f"{command[0]} is not installed"), file=sys.stderr)
        return False
    started = time.monotonic()
    # Fixed commands from this module; no user input reaches the argument list.
    result = subprocess.run(  # noqa: S603
        [tool, *command[1:]], cwd=cwd, check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(style.warning(f"`{label}` failed"), file=sys.stderr)
        print((result.stderr or result.stdout or "").strip()[-2000:], file=sys.stderr)
        return False
    print(style.ok(f"{label} {style.dim(f'({time.monotonic() - started:.1f}s)')}"))
    return True


def next_steps(style: Style, target: Path, name: str, synced: bool) -> list[str]:
    commands = [f"cd {target}"]
    if not synced:
        commands.append("uv sync")
    commands += ["uv run pytest", f"uv run hinoki dev {name}.main:app"]
    if not style.color:
        return [f"next: {' && '.join(commands[:-1])}"]
    return ["", "  Next:", *(f"    {style.paint(c, 'brand')}" for c in commands)]


def run_new(args: argparse.Namespace, ask: Ask = input) -> int:
    style = Style.detect()
    if interactive(args):
        print(style.banner("new project"))
        try:
            fill_from_prompts(args, ask, style)
        except (EOFError, KeyboardInterrupt):
            print(f"\n{style.error('cancelled; nothing was created')}", file=sys.stderr)
            return 130
        print()
    if args.name is None:
        print(
            style.error("a project name is required (or run in a terminal)"),
            file=sys.stderr,
        )
        return 2
    try:
        target = create(
            args.name,
            Path(args.dir or "."),
            args.hinoki,
            database=args.database or "none",
            agent_files=args.agent_files is not False,
        )
    except ScaffoldError as error:
        print(style.error(str(error)), file=sys.stderr)
        return 2
    count = sum(1 for path in target.rglob("*") if path.is_file())
    print(style.ok(f"created {target} {style.dim(f'({count} files)')}"))
    synced = False
    if args.git:
        run_tool(["git", "init"], target, style)
    if args.install:
        synced = run_tool(["uv", "sync"], target, style)
    for line in next_steps(style, target, args.name, synced):
        print(line)
    return 0
