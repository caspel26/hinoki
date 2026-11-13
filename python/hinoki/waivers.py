"""Owner-declared exceptions to rules (``hinoki-rules.toml``).

    [[waiver]]
    rule = "HNK-ACC-001"
    target = "blog.api.health"        # fnmatch on the diagnostic's subject
    reason = "liveness probe is public by design"
    owner = "platform-team"
    expires = "2027-01-31"            # optional ISO date

A waiver names a rule, a bounded target, a reason and an owner; a bare ``*``
target is refused. An expired waiver no longer applies and says so. Tools read
this file and report waived findings, they never write it: changing an
exception is a decision for the project owner, made in review.
"""

from __future__ import annotations

import dataclasses
import datetime
import fnmatch
import tomllib
from pathlib import Path
from typing import Any

from hinoki.rules import Diagnostic, Phase, Severity, Status

NAME = "hinoki-rules.toml"
STALE = "HNK-WVR-001"
EXPIRED = "HNK-WVR-002"
REQUIRED = ("rule", "target", "reason", "owner")


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
        return self.rule == diagnostic.rule_id and fnmatch.fnmatchcase(
            diagnostic.subject, self.target
        )


def parse_waiver(index: int, raw: dict[str, Any]) -> Waiver:
    missing = [key for key in REQUIRED if not str(raw.get(key, "")).strip()]
    if missing:
        raise WaiverError(f"waiver #{index}: missing {', '.join(missing)}")
    unknown = set(raw) - {*REQUIRED, "expires"}
    if unknown:
        raise WaiverError(f"waiver #{index}: unknown keys {sorted(unknown)}")
    target = str(raw["target"]).strip()
    if target.strip("*?") == "":
        raise WaiverError(f"waiver #{index}: the target {target!r} covers everything")
    expires = raw.get("expires")
    if expires is not None and not isinstance(expires, datetime.date):
        raise WaiverError(f"waiver #{index}: expires must be a date such as 2027-01-31")
    if isinstance(expires, datetime.datetime):
        raise WaiverError(f"waiver #{index}: expires must be a date, not a time")
    return Waiver(
        str(raw["rule"]).strip(),
        target,
        str(raw["reason"]).strip(),
        str(raw["owner"]).strip(),
        expires,
    )


def load_waivers(path: Path) -> list[Waiver]:
    """The waivers in ``path``; an absent file means none."""
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise WaiverError(f"{path}: {exc}") from exc
    entries = data.get("waiver", [])
    if not isinstance(entries, list):
        raise WaiverError(f"{path}: [[waiver]] must be a list of tables")
    return [parse_waiver(i, dict(entry)) for i, entry in enumerate(entries, 1)]


def notice(rule: str, message: str, subject: str) -> Diagnostic:
    return Diagnostic(
        rule,
        Severity.WARNING,
        Phase.STARTUP,
        message,
        NAME,
        "Waivers are exceptions with an owner and a reason; keep them current.",
        Status.VIOLATED,
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
