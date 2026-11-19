"""``hinoki new``: create a project, interactively when run from a terminal."""

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from hinoki.scaffold import DATABASES, NAME, RESERVED_NAMES, ScaffoldError, create

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


def ask_text(ask: Ask, question: str, default: str) -> str:
    answer = ask(f"{question} [{default}]: ").strip()
    return answer or default


def ask_choice(ask: Ask, question: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        answer = ask_text(ask, f"{question} ({'/'.join(choices)})", default)
        if answer in choices:
            return answer
        print(f"choose one of: {', '.join(choices)}", file=sys.stderr)


def ask_yes(ask: Ask, question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{question} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("answer y or n", file=sys.stderr)


def ask_name(ask: Ask) -> str:
    while True:
        name = ask_text(ask, "Project name (lower-case letters and digits)", "")
        if NAME.fullmatch(name) and name not in RESERVED_NAMES:
            return name
        print(
            "use lower-case letters and digits, starting with a letter; "
            "not 'hinoki' or a standard-library module name",
            file=sys.stderr,
        )


def interactive(args: argparse.Namespace) -> bool:
    return not args.no_input and sys.stdin.isatty() and sys.stdout.isatty()


def fill_from_prompts(args: argparse.Namespace, ask: Ask) -> None:
    """Ask only for what the flags left unset."""
    if args.name is None:
        args.name = ask_name(ask)
    if args.dir is None:
        args.dir = ask_text(ask, "Parent directory", ".")
    if args.database is None:
        args.database = ask_choice(ask, "Database", DATABASES, "none")
    if args.agent_files is None:
        args.agent_files = ask_yes(
            ask, "Write agent instructions and MCP config?", True
        )
    if args.git is None:
        args.git = ask_yes(ask, "Initialize a git repository?", True)
    if args.install is None:
        args.install = ask_yes(ask, "Install dependencies now (uv sync)?", True)


def run_tool(command: list[str], cwd: Path) -> bool:
    """Run one setup command; a missing tool or a failure is reported, not fatal."""
    tool = shutil.which(command[0])
    if tool is None:
        print(f"skipped: {command[0]} is not installed", file=sys.stderr)
        return False
    # Fixed commands from this module; no user input reaches the argument list.
    result = subprocess.run([tool, *command[1:]], cwd=cwd, check=False)  # noqa: S603
    if result.returncode != 0:
        print(f"warning: `{' '.join(command)}` failed", file=sys.stderr)
    return result.returncode == 0


def run_new(args: argparse.Namespace, ask: Ask = input) -> int:
    if interactive(args):
        fill_from_prompts(args, ask)
    if args.name is None:
        print(
            "error: a project name is required (or run in a terminal)", file=sys.stderr
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
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"created {target}")
    synced = False
    if args.git:
        run_tool(["git", "init", "-q"], target)
    if args.install:
        synced = run_tool(["uv", "sync"], target)
    prefix = "" if synced else "uv sync && "
    print(f"next: cd {target} && {prefix}uv run pytest")
    return 0
