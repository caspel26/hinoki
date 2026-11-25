"""Owner-declared exceptions to rules (``hinoki-rules.toml``).

    [[waiver]]
    rule = "HNK-ACC-001"
    target = "blog.api.health"        # fnmatch on the diagnostic's subject
    reason = "liveness probe is public by design"
    owner = "platform-team"
    expires = 2027-01-31              # optional TOML date (unquoted), UTC

A waiver names a rule, a bounded target, a reason and an owner; a bare ``*``
target is refused. An expired waiver no longer applies and says so. Tools read
this file and report waived findings, they never write it: changing an
exception is a decision for the project owner, made in review.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import tomllib
from pathlib import Path
from typing import Any

from hinoki import core
from hinoki.project import find_manifest_or_none
from hinoki.rules import Diagnostic, Phase, Severity, Status

NAME = "hinoki-rules.toml"
STALE = "HNK-WVR-001"
EXPIRED = "HNK-WVR-002"
REQUIRED = ("rule", "target", "reason", "owner")
MIN_PREFIX = 5


class WaiverError(ValueError):
    """The waiver file is invalid; the check must fail rather than guess."""


@dataclasses.dataclass(frozen=True, slots=True)
class Waiver:
    rule: str
    target: str
    reason: str
    owner: str
    expires: datetime.date | None = None

    def describe(self) -> str:
        until = f", until {self.expires}" if self.expires else ""
        return f"{self.reason} ({self.owner}{until})"

    def covers(self, diagnostic: Diagnostic) -> bool:
        """An empty subject marks "the rule could not do its job"; such a
        diagnostic cannot be waived."""
        return (
            bool(diagnostic.subject)
            and self.rule == diagnostic.rule_id
            and core.fnmatch_case(diagnostic.subject, self.target)
        )


def parse_waiver(index: int, raw: dict[str, Any]) -> Waiver:
    try:
        rule, target, reason, owner, expires = core.validate_waiver(index, raw)
    except ValueError as exc:
        raise WaiverError(str(exc)) from None
    return Waiver(rule, target, reason, owner, expires)


def load_waivers(path: Path) -> list[Waiver]:
    """The waivers in ``path``; nothing at ``path`` means none. Anything else
    that cannot be read as a waiver file is a ``WaiverError``."""
    if not os.path.lexists(path):
        return []
    if not path.is_file():
        raise WaiverError(
            f"{path} is not a regular file (a directory or a broken symbolic link)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WaiverError(f"{path}: cannot read: {exc}") from exc
    try:
        return parse_waivers(tomllib.loads(text))
    except (TypeError, ValueError) as exc:  # includes WaiverError and TOML errors
        raise WaiverError(f"{path}: {exc}") from exc


def parse_waivers(data: dict[str, Any]) -> list[Waiver]:
    entries = data.get("waiver", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise WaiverError("[[waiver]] must be a list of tables")
    return [parse_waiver(i, entry) for i, entry in enumerate(entries, 1)]


def waiver_path(path: str | Path | None = None) -> Path:
    """The waiver file: ``path`` when named, else ``hinoki-rules.toml`` at the
    project root (the directory of the nearest ``hinoki.toml`` from the working
    directory upwards), else in the working directory."""
    if path is not None:
        return Path(path)
    manifest = find_manifest_or_none()
    return (manifest.parent if manifest else Path.cwd()) / NAME


def read_waivers(path: str | Path | None = None) -> list[Waiver]:
    """The waivers ``hinoki check``, ``hinoki mcp`` and application startup
    apply (see ``waiver_path``). An absent default file means none; a file
    named explicitly must exist."""
    if path is not None and not os.path.lexists(path):
        raise WaiverError(f"waiver file {path} does not exist")
    return load_waivers(waiver_path(path))


def notice(rule: str, message: str, subject: str) -> Diagnostic:
    return Diagnostic(
        rule,
        Severity.WARNING,
        Phase.STARTUP,
        message,
        NAME,
        "Waivers are exceptions with an owner and a reason; keep them current.",
        Status.WARNING,
        subject,
    )


def apply_waivers(
    diagnostics: list[Diagnostic], waivers: list[Waiver], today: datetime.date
) -> list[Diagnostic]:
    """Mark covered diagnostics ``waived``; add notices for waivers that expired
    or matched nothing. Expired waivers do not apply."""
    live = [w for w in waivers if w.expires is None or w.expires >= today]
    result = list(diagnostics)
    used: set[Waiver] = set()
    for index, found in enumerate(result):
        owner = next((w for w in live if w.covers(found)), None)
        if owner is not None:
            used.add(owner)
            result[index] = dataclasses.replace(
                found, status=Status.WAIVED, waiver=owner.describe()
            )
    for waiver in waivers:
        if waiver not in live:
            result.append(
                notice(
                    EXPIRED,
                    f"waiver for {waiver.rule} {waiver.target} "
                    f"expired on {waiver.expires}",
                    waiver.target,
                )
            )
        elif waiver not in used:
            result.append(
                notice(
                    STALE,
                    f"waiver for {waiver.rule} {waiver.target} "
                    "matches nothing; remove it",
                    waiver.target,
                )
            )
    return result
