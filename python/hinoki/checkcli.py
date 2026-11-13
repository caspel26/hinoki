"""``python -m hinoki check``: run the project's rules and report diagnostics.

Builds the application registry (importing the project's module, which is
application code) without starting lifespans or touching a database or the
network. Exit codes: 0 clean, 1 violations, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import sys
from pathlib import Path

from hinoki.app import HinokiAPI
from hinoki.errors import RegistrationError
from hinoki.rules import (
    SCHEMA_VERSION,
    Diagnostic,
    Phase,
    Project,
    RuleSet,
    Status,
    failing,
)
from hinoki.waivers import NAME, WaiverError, apply_waivers, load_waivers

USAGE_ERRORS = (ImportError, AttributeError, ValueError, TypeError, RegistrationError)


def load_object(spec: str) -> object:
    """``module:attribute`` (dotted attributes allowed)."""
    module, _, attribute = spec.partition(":")
    if not module or not attribute:
        raise ValueError(f"expected module:attribute, got {spec!r}")
    found: object = importlib.import_module(module)
    for part in attribute.split("."):
        found = getattr(found, part)
    return found


def build_app(spec: str) -> HinokiAPI:
    found = load_object(spec)
    if callable(found) and not isinstance(found, HinokiAPI):
        found = found()
    if not isinstance(found, HinokiAPI):
        raise TypeError(f"{spec} is not a HinokiAPI or a factory returning one")
    return found


def build_rules(spec: str | None, app: HinokiAPI) -> RuleSet:
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


def render(diagnostics: list[Diagnostic], rule_count: int, fmt: str) -> str:
    totals = summary(diagnostics)
    if fmt == "json":
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "rules": rule_count,
                "summary": totals,
                "diagnostics": [d.as_dict() for d in diagnostics],
            },
            indent=2,
        )
    lines = [d.text() for d in diagnostics]
    lines.extend(f"    waived: {d.waiver}" for d in diagnostics if d.waiver)
    lines.append(
        f"{rule_count} rule(s): {totals['violated']} violated, "
        f"{totals['unverifiable']} unverifiable, {totals['waived']} waived"
    )
    return "\n".join(lines)


def run_check(args: argparse.Namespace, today: datetime.date | None = None) -> int:
    try:
        app = build_app(args.app)
        rules = build_rules(args.rules, app)
        project = Project(app, tuple(args.package or ()))
        phase = Phase(args.phase) if args.phase else None
        wanted = Path(args.waivers or NAME)
        if args.waivers and not wanted.is_file():
            raise WaiverError(f"waiver file {wanted} does not exist")
        waivers = load_waivers(wanted)
        found = rules.run(project, phase, allow_effects=args.allow_effects)
        found = apply_waivers(found, waivers, today or datetime.date.today())
    except (*USAGE_ERRORS, WaiverError, SystemExit) as exc:  # factories may exit
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render(found, len(rules.rules), args.format))
    return 1 if failing(found) else 0


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    check = commands.add_parser("check", help="run the project's rules")
    check.add_argument("--app", required=True, help="module:factory or module:app")
    check.add_argument("--rules", help="module:ruleset (default: the app's rules)")
    check.add_argument("--package", action="append", help="package to scan (static)")
    check.add_argument("--phase", choices=[p.value for p in Phase])
    check.add_argument("--waivers", help=f"waiver file (default ./{NAME})")
    check.add_argument("--format", choices=["text", "json"], default="text")
    check.add_argument(
        "--allow-effects",
        action="store_true",
        help="also run rules that execute code or touch files, network or databases",
    )

    new = commands.add_parser("new", help="create a project an AI agent can work in")
    new.add_argument("name", help="package name: lower-case letters and digits")
    new.add_argument("--dir", default=".", help="parent directory (default: here)")
    server = commands.add_parser(
        "mcp", help="serve read-only project context over MCP (stdio)"
    )
    server.add_argument("--app", required=True, help="module:factory or module:app")
    server.add_argument("--rules", help="module:ruleset (default: the app's rules)")
    server.add_argument("--package", action="append", help="package to scan (static)")
    server.add_argument("--models", action="append", help="module that defines models")
    server.add_argument("--waivers", help=f"waiver file (default ./{NAME})")
