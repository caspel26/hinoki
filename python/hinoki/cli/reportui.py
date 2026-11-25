"""Coloured text for ``hinoki doctor``, ``hinoki check`` and ``hinoki routes``.

Each function is only used when the stream supports colour; the plain text of
those commands is produced by their own modules and does not change.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from hinoki.terminal import Style

DOCTOR_GLYPHS = {"ok": "ok", "warning": "warn", "failed": "bad", "skipped": "skip"}
CHECK_GLYPHS = {
    "violated": "bad",
    "unverifiable": "unsure",
    "waived": "skip",
    "warning": "warn",
}
METHOD_COLORS = {
    "GET": "good",
    "POST": "info",
    "PUT": "warn",
    "PATCH": "warn",
    "DELETE": "bad",
    "QUERY": "brand",
}


class DoctorCheck(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def message(self) -> str: ...
    @property
    def remediation(self) -> str | None: ...


class FoundDiagnostic(Protocol):
    @property
    def location(self) -> str: ...
    @property
    def rule_id(self) -> str: ...
    @property
    def status(self) -> object: ...
    @property
    def message(self) -> str: ...
    @property
    def fix(self) -> str | None: ...
    @property
    def waiver(self) -> str | None: ...


def doctor_text(style: Style, checks: Sequence[DoctorCheck], note: str) -> str:
    lines = [f"\n  {style.paint('Hinoki doctor', 'brand', bold=True)}\n"]
    for check in checks:
        row = f"{check.id:<25} {check.message}"
        lines.append(style.line(DOCTOR_GLYPHS[check.status], row))
        if check.remediation:
            lines.append(
                f"       {style.dim(style.symbol('→', '->'))} {check.remediation}"
            )
    lines.append(f"\n  {style.dim(note)}\n")
    return "\n".join(lines) + "\n"


def check_summary(rules: int, totals: dict[str, int]) -> str:
    line = (
        f"{rules} rule(s): {totals['violated']} violated, "
        f"{totals['unverifiable']} unverifiable, {totals['waived']} waived"
    )
    warnings = totals.get("warning", 0)
    return f"{line}, {warnings} warning(s)" if warnings else line


def check_text(
    style: Style, found: Sequence[FoundDiagnostic], rules: int, totals: dict[str, int]
) -> str:
    lines = []
    for item in found:
        glyph = CHECK_GLYPHS[str(item.status)]
        head = f"{style.bold(item.rule_id)} {style.dim(item.location)}"
        lines.append(style.line(glyph, head))
        lines.append(f"       {item.message}")
        if item.fix:
            lines.append(f"       {style.dim('fix:')} {item.fix}")
        if item.waiver:
            lines.append(f"       {style.dim('waived: ' + item.waiver)}")
    summary = check_summary(rules, totals)
    clean = totals["failing"] == 0  # green exactly when the exit code is 0
    lines.append("")
    lines.append(style.ok(summary) if clean else style.error(summary))
    return "\n".join(lines)


def route_row(
    style: Style,
    method: str,
    path: str,
    width: int,
    access: str,
    handler: str,
    access_width: int = 24,
) -> str:
    verb = style.paint(f"{method:<7}", METHOD_COLORS.get(method, "text"), bold=True)
    pad = " " * (width - len(path))
    shown = style.paint(access, "dim" if access == "public" else "brand")
    spacing = " " * max(access_width - len(access), 0)
    return f"{verb} {style.text(path)}{pad}  {shown}{spacing} {style.dim(handler)}"


def lane_suffix(style: Style, lane: str, reason: str) -> str:
    colour = "good" if lane == "native" else "dim"
    return "  " + style.paint(f"[{lane}: {reason}]", colour)


def run_timed(doing: str, done: str, action: Callable[[], int]) -> int:
    """Run ``action`` behind a spinner and finish with how long it took.

    Only on a terminal that animates; elsewhere ``action`` runs unchanged. Its
    output is held back until the spinner is cleared so the two never mix.
    """
    style = Style.detect(sys.stdout)
    if not (style.animated and style.unicode):
        return action()
    held = io.StringIO()
    started = time.monotonic()
    code = 1
    try:
        with style.spinner(doing), contextlib.redirect_stdout(held):
            code = action()
    finally:
        sys.stdout.write(held.getvalue())
    result = style.timed(done, time.monotonic() - started)
    print(style.ok(result) if code == 0 else style.error(result))
    return code
