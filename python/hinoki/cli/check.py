"""``hinoki check``: run the project's rules and report diagnostics.

Builds the application registry (importing the project's module, which is
application code) without starting lifespans or touching a database or the
network. Exit codes: 0 clean, 1 violations, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import datetime
import sys

from hinoki import jsontext
from hinoki.app import Hinoki
from hinoki.cli.new import add_arguments as add_new_arguments
from hinoki.cli.reportui import check_summary, check_text
from hinoki.errors import RegistrationError
from hinoki.mail import TemplateError
from hinoki.orm.links import check_resolved
from hinoki.project import find_manifest_or_none, load_manifest
from hinoki.rules import (
    SCHEMA_VERSION,
    Diagnostic,
    Phase,
    Project,
    RuleSet,
    Status,
    failing,
)
from hinoki.target import load_target
from hinoki.terminal import Style
from hinoki.utils import utc_today
from hinoki.waivers import NAME, WaiverError, apply_waivers, read_waivers

USAGE_ERRORS = (
    ImportError,
    AttributeError,
    ValueError,
    TypeError,
    RegistrationError,
    TemplateError,
)
WAIVERS_HELP = (
    f"waiver file (default: Hinoki(waivers=...), else {NAME} at the project root)"
)
NO_RULES_WARNING = (
    "warning: no rules were run, so this check proves nothing; declare them with "
    "Hinoki(rules=RuleSet(...)) or pass --rules module:ruleset"
)


def load_object(spec: str) -> object:
    """``module:attribute`` (dotted attributes allowed)."""
    return load_target(spec)


def build_app(spec: str) -> Hinoki:
    found = load_object(spec)
    if callable(found) and not isinstance(found, Hinoki):
        found = found()
    if not isinstance(found, Hinoki):
        raise TypeError(f"{spec} is not a Hinoki or a factory returning one")
    return found


def default_packages(target: str) -> tuple[str, ...]:
    """The top-level package of the target module: what the import rules scan
    when ``--package`` is not given."""
    return (target.partition(":")[0].split(".")[0],)


def import_project_models(target: str) -> None:
    """Import the models of the project that declares ``target``, so the model
    rules see them. Another target, or no manifest, imports nothing."""
    path = find_manifest_or_none()
    if path is None:
        return
    manifest = load_manifest(path)
    if manifest.application != target:
        return
    for app in manifest.ordered_apps():
        manifest.models(app)


def build_rules(spec: str | None, app: Hinoki) -> RuleSet:
    if spec is None:
        return app.rules or RuleSet()
    found = load_object(spec)
    if callable(found) and not isinstance(found, RuleSet):
        found = found()
    if not isinstance(found, RuleSet):
        raise TypeError(f"{spec} is not a RuleSet")
    return found


def summary(diagnostics: list[Diagnostic]) -> dict[str, int]:
    counts = {status.value: 0 for status in Status}
    for found in diagnostics:
        counts[found.status.value] += 1
    return {**counts, "failing": len(failing(diagnostics))}


def render(
    diagnostics: list[Diagnostic],
    rule_count: int,
    fmt: str,
    style: Style | None = None,
) -> str:
    totals = summary(diagnostics)
    if fmt == "json":
        return jsontext.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "rules": rule_count,
                "summary": totals,
                "diagnostics": [d.as_dict() for d in diagnostics],
            },
            indent=2,
        )
    if style is not None and style.color:
        return check_text(style, diagnostics, rule_count, totals)
    lines = []
    for found in diagnostics:
        lines.append(found.text())
        if found.waiver:
            lines.append(f"    waived: {found.waiver}")
    lines.append(check_summary(rule_count, totals))
    return "\n".join(lines)


def run_check(args: argparse.Namespace, today: datetime.date | None = None) -> int:
    try:
        app = build_app(args.target)
        import_project_models(args.target)
        check_resolved()
        rules = build_rules(args.rules, app)
        project = Project(app, tuple(args.package or default_packages(args.target)))
        phase = Phase(args.phase) if args.phase else None
        waivers = read_waivers(args.waivers or app.waiver_file)
        found = rules.run(project, phase, allow_effects=args.allow_effects)
        found = apply_waivers(found, waivers, today or utc_today())
    except (*USAGE_ERRORS, WaiverError, SystemExit) as exc:  # factories may exit
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render(found, len(rules.rules), args.format, Style.detect(sys.stdout)))
    if not rules.rules:
        print(NO_RULES_WARNING, file=sys.stderr)
    return 1 if failing(found) else 0


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    check = commands.add_parser("check", help="run the project's rules")
    check.add_argument(
        "target", nargs="?", help="module:app or module:factory (default: project app)"
    )
    check.add_argument("--rules", help="module:ruleset (default: the app's rules)")
    check.add_argument("--package", action="append", help="package to scan (static)")
    check.add_argument("--phase", choices=[p.value for p in Phase])
    check.add_argument("--waivers", help=WAIVERS_HELP)
    check.add_argument("--format", choices=["text", "json"], default="text")
    check.add_argument(
        "--allow-effects",
        action="store_true",
        help="also run rules that execute code or touch files, network or databases",
    )

    new = commands.add_parser("new", help="create a project an AI agent can work in")
    add_new_arguments(new)
    server = commands.add_parser(
        "mcp", help="serve project context to a coding agent over MCP (stdio)"
    )
    server.add_argument(
        "target", nargs="?", help="module:app or module:factory (default: project app)"
    )
    server.add_argument("--rules", help="module:ruleset (default: the app's rules)")
    server.add_argument("--package", action="append", help="package to scan (static)")
    server.add_argument("--models", action="append", help="module that defines models")
    server.add_argument("--waivers", help=WAIVERS_HELP)
    server.add_argument(
        "--requests",
        help="file of recent requests (default ./.hinoki/requests.jsonl)",
    )
    server.add_argument(
        "--scenarios",
        choices=["off", "read", "write"],
        default="off",
        help="offer run_scenario: read = GET, HEAD, OPTIONS, QUERY, write = any method "
        "(runs real handlers: use a disposable database)",
    )
