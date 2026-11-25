"""Consolidated, non-invasive project diagnostics for ``hinoki doctor``."""

from __future__ import annotations

import argparse
import importlib.metadata
import platform
import sys
from dataclasses import asdict, dataclass
from typing import Literal

import hinoki
import hinoki.core as native_core
from hinoki import jsontext
from hinoki.cli.check import build_app
from hinoki.cli.reportui import doctor_text
from hinoki.errors import RegistrationError
from hinoki.project import (
    ProjectError,
    ProjectManifest,
    add_project_root,
    find_manifest_or_none,
    load_manifest,
)
from hinoki.settings import Settings
from hinoki.terminal import Style

Status = Literal["ok", "warning", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    status: Status
    message: str
    remediation: str | None = None


def add_arguments(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    command = commands.add_parser(
        "doctor", help="diagnose the local Hinoki project and environment"
    )
    command.add_argument(
        "target",
        nargs="?",
        help="module:app or module:factory (imports application Python code)",
    )
    command.add_argument("--format", choices=["text", "json"], default="text")
    command.epilog = (
        "Application diagnostics import the selected Python target. Doctor does "
        "not connect to a database, write files, run rules, start a server, or "
        "call lifecycle hooks."
    )


def _check_manifest() -> tuple[Check, ProjectManifest | None]:
    path = find_manifest_or_none()
    if path is None:
        return Check("project.manifest", "skipped", "no hinoki.toml found"), None
    try:
        manifest = load_manifest(path)
    except (ProjectError, OSError, UnicodeError, ValueError) as exc:
        return (
            Check(
                "project.manifest",
                "failed",
                f"hinoki.toml is invalid ({type(exc).__name__})",
                "Correct hinoki.toml and rerun `hinoki doctor`.",
            ),
            None,
        )
    return (
        Check(
            "project.manifest",
            "ok",
            f"{len(manifest.apps)} app(s) registered",
        ),
        manifest,
    )


def _environment_checks() -> list[Check]:
    checks = [
        Check("environment.hinoki", "ok", f"Hinoki {hinoki.__version__}"),
        Check("environment.python", "ok", f"Python {platform.python_version()}"),
        Check(
            "environment.extension",
            "ok" if native_core.__file__ else "warning",
            "native extension loaded"
            if native_core.__file__
            else "native extension path is unavailable",
        ),
    ]
    try:
        importlib.metadata.version("granian")
    except importlib.metadata.PackageNotFoundError:
        checks.append(
            Check(
                "environment.server",
                "warning",
                "Granian is not installed (needed for --server granian)",
                "Install Hinoki with its server extra to serve with Granian.",
            )
        )
    else:
        checks.append(Check("environment.server", "ok", "Granian is installed"))
    return checks


TARGET_HINT = (
    "Check the module:attribute target and inspect it with `hinoki routes TARGET`."
)
BUILD_HINT = (
    "The application could not be built; the message above gives the cause "
    "(`hinoki routes TARGET` fails with the same error)."
)


def _load_failure(exc: BaseException, limit: int = 300) -> tuple[str, str]:
    """Summary and fix of a load failure. The message of a ``RegistrationError``
    with no chained cause is shown on one line, truncated; any other error, or a
    chain that may come from a settings value, shows only its class name."""
    name = type(exc).__name__
    if not isinstance(exc, RegistrationError) or exc.__cause__ is not None:
        return f"target could not be loaded ({name})", TARGET_HINT
    text = " ".join(str(exc).split())
    shown = text if len(text) <= limit else text[: limit - 3] + "..."
    return f"target could not be loaded ({name}: {shown})", BUILD_HINT


def _target_check(
    target: str, manifest: ProjectManifest | None
) -> tuple[list[Check], int]:
    if manifest is not None:
        add_project_root(manifest)
    try:
        app = build_app(target)
    except (Exception, SystemExit) as exc:
        summary, fix = _load_failure(exc)
        return (
            [
                Check("application.load", "failed", summary, fix),
                Check("application.routes", "skipped", "application did not load"),
                Check("application.settings", "skipped", "application did not load"),
                Check("application.openapi", "skipped", "application did not load"),
            ],
            2,
        )

    checks = [Check("application.load", "ok", "application loaded")]
    checks.append(
        Check("application.routes", "ok", f"{len(app.routes)} route(s) registered")
    )
    if isinstance(app.settings, Settings):
        checks.append(Check("application.settings", "ok", "typed settings validated"))
    elif app.settings is None:
        checks.append(
            Check("application.settings", "skipped", "no settings object configured")
        )
    else:
        checks.append(
            Check(
                "application.settings",
                "warning",
                "custom settings object is not a Hinoki Settings instance",
            )
        )
    try:
        app.openapi()
    except Exception as exc:  # user-defined schema hooks can raise arbitrary errors
        checks.append(
            Check(
                "application.openapi",
                "failed",
                f"OpenAPI generation failed ({type(exc).__name__})",
                "Inspect route and Schema declarations, then run "
                "`hinoki openapi TARGET` for details.",
            )
        )
        return checks, 1
    checks.append(Check("application.openapi", "ok", "document generated in memory"))
    return checks, 0


def collect(target: str | None) -> tuple[list[Check], int]:
    checks = _environment_checks()
    manifest_check, manifest = _check_manifest()
    checks.append(manifest_check)

    selected = target
    if selected is None and manifest is not None:
        selected = manifest.application
    if manifest_check.status == "failed" and target is None:
        checks.extend(
            Check(identifier, "skipped", "manifest validation failed")
            for identifier in (
                "application.load",
                "application.routes",
                "application.settings",
                "application.openapi",
            )
        )
        return checks, 1
    if selected is None:
        checks.extend(
            Check(identifier, "skipped", "no application target selected")
            for identifier in (
                "application.load",
                "application.routes",
                "application.settings",
                "application.openapi",
            )
        )
        checks.append(
            Check(
                "next_step",
                "warning",
                "pass TARGET or set project.application in hinoki.toml",
            )
        )
        return checks, 0
    app_checks, code = _target_check(selected, manifest if target is None else None)
    checks.extend(app_checks)
    return checks, max(code, int(manifest_check.status == "failed"))


NOTE = (
    "Database and migration state were not checked; use `hinoki migrations "
    "status` and `hinoki migrations plan --check` when needed."
)


def render(checks: list[Check], fmt: str, style: Style | None = None) -> str:
    if fmt == "json":
        return (
            jsontext.dumps(
                {"schema_version": 1, "checks": [asdict(check) for check in checks]},
                indent=2,
            )
            + "\n"
        )
    if style is not None and style.color:
        return doctor_text(style, checks, NOTE)
    lines = ["Hinoki doctor"]
    for check in checks:
        suffix = f" — {check.remediation}" if check.remediation else ""
        lines.append(f"{check.status:<7} {check.id:<25} {check.message}{suffix}")
    lines.append(NOTE)
    return "\n".join(lines) + "\n"


def run_doctor(args: argparse.Namespace) -> int:
    checks, code = collect(args.target)
    print(
        render(checks, args.format, Style.detect(sys.stdout)), end="", file=sys.stdout
    )
    return code
