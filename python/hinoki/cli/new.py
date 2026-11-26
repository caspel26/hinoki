"""``hinoki new``: create a project, interactively when run from a terminal."""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from hinoki import __version__
from hinoki.cli.prompts import Check, Prompter, keys_available, raw_keys
from hinoki.scaffold import (
    DATABASES,
    NAME,
    REPOSITORY,
    RESERVED_NAMES,
    SERVERS,
    ScaffoldError,
    create,
)
from hinoki.terminal import Style, elapsed, shown_path

Ask = Callable[[str], str]


def add_arguments(new: argparse.ArgumentParser) -> None:
    new.add_argument(
        "name",
        nargs="?",
        help="package name: lower-case letters, digits, single underscores",
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
        "--server",
        choices=SERVERS,
        default=None,
        help="server that `hinoki dev` and `hinoki serve` run (default: kiso)",
    )
    new.add_argument(
        "--agent-files",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="write AGENTS.md, CLAUDE.md and .mcp.json (default: yes)",
    )
    new.add_argument(
        "--skills",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="write optional agent skills under .claude/skills (default: no)",
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
    apps = new.add_mutually_exclusive_group()
    apps.add_argument(
        "--app",
        default=None,
        metavar="NAME",
        help="also create this app, without asking (default: asked for in a terminal)",
    )
    apps.add_argument(
        "--no-app",
        action="store_true",
        help="create no app and do not ask about one",
    )
    new.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt; use the flags and defaults",
    )


DOCS = "https://hinoki.caspel.dev/"
DEFAULT_APP = "core"
DATABASE_OPTIONS = (
    ("none", "no database setting"),
    ("sqlite", "local file in .hinoki/"),
    ("postgresql", "also writes .env.example"),
)
SERVER_OPTIONS = (
    ("kiso", "Hinoki's own Rust server: native handler lane, access log"),
    ("granian", "general-purpose ASGI server: runs any ASGI app"),
)
KEY_FILES = (
    ("{name}/main.py", "the application"),
    ("{name}/routes.py", "route map"),
    ("{name}/api.py", "endpoints"),
    ("{name}/settings.py", "typed settings"),
    ("{name}/rules.py", "architecture rules"),
    ("hinoki.toml", "project registry: apps, models, migrations"),
    ("tests/", "pytest suite"),
    ("AGENTS.md", "agent instructions"),
    (".env.example", "database URL template"),
)


class Questions(Protocol):
    """What ``fill`` asks: line by line or with the key-driven prompts."""

    def text(
        self, label: str, summary: str, default: str, check: Check, hint: str = ""
    ) -> str: ...

    def choice(
        self, label: str, summary: str, options: Sequence[tuple[str, str]], default: str
    ) -> str: ...

    def confirm(self, label: str, summary: str, default: bool) -> bool: ...


def ask_text(ask: Ask, style: Style, question: str, default: str) -> str:
    return ask(style.question(question, default)).strip() or default


def ask_choice(
    ask: Ask,
    style: Style,
    question: str,
    options: Sequence[tuple[str, str]],
    default: str,
) -> str:
    """Numbered choices; the answer is a number or the name."""
    names = [name for name, _ in options]
    print(style.heading(question))
    for index, (name, hint) in enumerate(options, start=1):
        print(style.option(index, name, hint, default=name == default))
    while True:
        answer = ask_text(ask, style, "Choose", default)
        if answer in names:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        print(style.warning(f"choose one of: {', '.join(names)}"), file=sys.stderr)


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


def check_name(name: str) -> str | None:
    if not name:
        return "a project name is required"
    if name in RESERVED_NAMES:
        return f"'{name}' is taken by hinoki or the standard library"
    if not NAME.fullmatch(name):
        return "use lower-case letters, digits and single underscores, from a letter"
    return None


def any_text(_text: str) -> str | None:
    return None


class LinePrompts:
    """Questions asked one line at a time with ``ask`` (``input`` by default)."""

    def __init__(self, ask: Ask, style: Style) -> None:
        self.ask_line, self.style = ask, style

    def text(
        self, label: str, _summary: str, default: str, check: Check, hint: str = ""
    ) -> str:
        question = f"{label} ({hint})" if hint else label
        while True:
            answer = ask_text(self.ask_line, self.style, question, default)
            problem = check(answer)
            if problem is None:
                return answer
            print(self.style.warning(problem), file=sys.stderr)

    def choice(
        self,
        label: str,
        _summary: str,
        options: Sequence[tuple[str, str]],
        default: str,
    ) -> str:
        return ask_choice(self.ask_line, self.style, label, options, default)

    def confirm(self, label: str, _summary: str, default: bool) -> bool:
        return ask_yes(self.ask_line, self.style, label, default)


def interactive(args: argparse.Namespace) -> bool:
    return not args.no_input and sys.stdin.isatty() and sys.stdout.isatty()


def fill(args: argparse.Namespace, questions: Questions) -> None:
    """Ask only for what the flags left unset."""
    if args.name is None:
        args.name = questions.text(
            "Project name", "Name", "", check_name, "lower-case letters and digits"
        )
    if args.dir is None:
        args.dir = questions.text("Parent directory", "Directory", ".", any_text)
    if args.database is None:
        args.database = questions.choice(
            "Database", "Database", DATABASE_OPTIONS, "none"
        )
    if args.server is None:
        args.server = questions.choice("Server", "Server", SERVER_OPTIONS, "kiso")
    if args.agent_files is None:
        args.agent_files = questions.confirm(
            "Agent instructions and MCP config?", "Agent files", True
        )
    if args.skills is None:
        args.skills = questions.confirm("Agent skills?", "Skills", False)
    if args.git is None:
        args.git = questions.confirm("Initialize a git repository?", "Git", True)
    if args.install is None:
        args.install = questions.confirm(
            "Install dependencies (uv sync)?", "Install", True
        )
    ask_app_name(args, questions)


def ask_app_name(args: argparse.Namespace, questions: Questions) -> None:
    """Offer to create the first app unless a flag already decided."""
    if args.app is not None or args.no_app:
        return
    if questions.confirm("Create an app now?", "App", True):
        args.app = questions.text(
            "App name",
            "App name",
            DEFAULT_APP,
            app_name_problem,
            "lower-case letters and digits",
        )


def app_name_problem(name: str) -> str | None:
    # appprompts imports this module, so the import cannot be at the top.
    from hinoki.cli.appprompts import name_check  # noqa: PLC0415

    return name_check(())(name)


def fill_from_prompts(
    args: argparse.Namespace, ask: Ask, style: Style | None = None
) -> None:
    """Ask line by line for what the flags left unset."""
    fill(args, LinePrompts(ask, style or Style()))


def run_tool(command: list[str], cwd: Path, style: Style) -> bool:
    """Run one setup command; a missing tool or a failure is reported, not fatal."""
    label = " ".join(command)
    tool = shutil.which(command[0])
    if tool is None:
        print(style.skipped(f"{command[0]} is not installed"), file=sys.stderr)
        return False
    started = time.monotonic()
    # Fixed commands from this module; no user input reaches the argument list.
    with style.spinner(label):
        result = subprocess.run(  # noqa: S603
            [tool, *command[1:]], cwd=cwd, check=False, capture_output=True, text=True
        )
    if result.returncode != 0:
        print(style.warning(f"`{label}` failed"), file=sys.stderr)
        print((result.stderr or result.stdout or "").strip()[-2000:], file=sys.stderr)
        return False
    if style.fancy:
        print(style.ok(style.timed(label, time.monotonic() - started)))
    else:
        took = elapsed(time.monotonic() - started)
        print(style.ok(f"{label} {style.dim(f'({took})')}"))
    return True


def key_files(style: Style, target: Path, name: str) -> list[str]:
    present = [
        (path.format(name=name), what)
        for path, what in KEY_FILES
        if (target / path.format(name=name)).exists()
    ]
    width = max(len(path) for path, _ in present) + 3
    lines = []
    for index, (path, what) in enumerate(present):
        branch = "\u2514\u2500" if index == len(present) - 1 else "\u251c\u2500"
        folder, _, file = path.rpartition("/")
        shown = style.paint(f"{folder}/", "light") if folder else ""
        padding = " " * (width - len(path))
        entry = (
            f"{style.dim(branch)} {shown}{style.text(file)}{padding}{style.dim(what)}"
        )
        lines.append(style.line("rail", entry))
    return lines


def report_created(style: Style, target: Path, name: str) -> None:
    count = sum(1 for path in target.rglob("*") if path.is_file())
    if not style.fancy:
        print(style.ok(f"created {target} {style.dim(f'({count} files)')}"))
        return
    created = f"Created {style.bold(name)}  {style.dim(f'{count} files')}"
    print(style.ok(created))
    root = style.paint(f"{shown_path(target)}{os.sep}", "light", bold=True)
    print(style.line("rail", root))
    for line in key_files(style, target, name):
        print(line)
    print(style.line("rail"))


def next_commands(target: Path, synced: bool) -> list[str]:
    commands = [f"cd {shown_path(target)}"]
    if not synced:
        commands.append("uv sync")
    return [*commands, "uv run pytest", "uv run hinoki dev"]


def next_steps(
    style: Style,
    target: Path,
    synced: bool,
    seconds: float = 0.0,
    app: str | None = None,
) -> list[str]:
    add_app = "uv run hinoki app create NAME"
    if not style.color:
        commands = [f"cd {target}", *next_commands(target, synced)[1:]]
        plain = f"next: {' && '.join(commands[:-1])}"
        return [plain if app else f"{plain}; add an app: {add_app}"]
    prompt = style.paint("$", "brand")
    ready = style.bold("Your project is ready")
    hint = [] if app else [style.line("rail", style.dim(f"add an app: {add_app}"))]
    return [
        style.line("rail"),
        style.line("active", style.bold("Next steps")),
        *(
            style.line("rail", f"{prompt} {style.text(command)}")
            for command in next_commands(target, synced)
        ),
        *hint,
        style.line("rail"),
        style.line("end", f"{ready}  {style.dim(elapsed(seconds))}"),
        f"     {style.dim('docs')}  {style.paint(DOCS, 'info')}",
        "",
    ]


def banner_facts() -> list[tuple[str, str]]:
    """What the banner of the interactive commands says about where they run."""
    return [
        ("python", platform.python_version()),
        ("docs", DOCS.removeprefix("https://").rstrip("/")),
        ("source", REPOSITORY.removeprefix("https://")),
    ]


def show_banner(style: Style, title: str = "new project") -> None:
    detail = f"v{__version__}"
    if style.animated and style.unicode:
        style.shimmer(title, detail, banner_facts())
    else:
        print(style.banner(title, detail, banner_facts()))
    if style.fancy:
        print(style.intro(title))


def cancelled(style: Style) -> str:
    if not style.fancy:
        return f"\n{style.error('cancelled; nothing was created')}"
    text = style.paint("Cancelled \u2014 nothing was created", "bad")
    return f"{style.line('end', text)}\n"


def ask_everything(args: argparse.Namespace, ask: Ask, style: Style) -> None:
    if not (style.fancy and keys_available()):
        fill_from_prompts(args, ask, style)
        return
    width = shutil.get_terminal_size().columns
    with raw_keys(sys.stdout) as keys:
        fill(args, Prompter(style, keys, sys.stdout, width))


def prompt(args: argparse.Namespace, ask: Ask, style: Style) -> int:
    show_banner(style)
    try:
        ask_everything(args, ask, style)
    except (EOFError, KeyboardInterrupt):
        print(cancelled(style), file=sys.stderr)
        return 130
    print(style.line("rail") if style.fancy else "")
    return 0


def create_first_app(style: Style, target: Path, name: str) -> bool:
    """Create the app the user asked for; a failure leaves the project in place."""
    # project imports this module, so the import cannot be at the top.
    from hinoki.cli.appprompts import report_app  # noqa: PLC0415
    from hinoki.cli.project import create_app  # noqa: PLC0415
    from hinoki.project import ProjectError  # noqa: PLC0415

    try:
        created = create_app(name, target)
    except (ProjectError, OSError) as error:
        print(style.error(str(error)), file=sys.stderr)
        print(
            style.warning(
                f"the project exists without the app; run `hinoki app create {name}`"
            ),
            file=sys.stderr,
        )
        return False
    for line in report_app(
        style,
        name,
        created,
        target / "hinoki.toml",
        (),
        next_steps=False,
    ):
        print(line)
    return True


def argument_problem(args: argparse.Namespace, style: Style) -> str | None:
    if args.name is None:
        return style.error("a project name is required (or run in a terminal)")
    if args.app is not None and (problem := app_name_problem(args.app)):
        return style.error(f"app name: {problem}")
    return None


def build_project(args: argparse.Namespace, style: Style) -> Path | None:
    """Scaffold the project and its first app; ``None`` after a reported failure."""
    try:
        target = create(
            args.name,
            Path(args.dir or "."),
            args.hinoki,
            database=args.database or "none",
            server=args.server or "kiso",
            agent_files=args.agent_files is not False,
            skills=args.skills is True,
        )
    except ScaffoldError as error:
        print(style.error(str(error)), file=sys.stderr)
        return None
    report_created(style, target, args.name)
    if args.app is not None and not create_first_app(style, target, args.app):
        return None
    return target


def run_new(args: argparse.Namespace, ask: Ask = input) -> int:
    style = Style.detect()
    if interactive(args) and (code := prompt(args, ask, style)):
        return code
    if problem := argument_problem(args, style):
        print(problem, file=sys.stderr)
        return 2
    started = time.monotonic()
    if (target := build_project(args, style)) is None:
        return 2
    synced = False
    if args.git:
        run_tool(["git", "init"], target, style)
    if args.install:
        synced = run_tool(["uv", "sync"], target, style)
    seconds = time.monotonic() - started
    for line in next_steps(style, target, synced, seconds, args.app):
        print(line)
    return 0
