from __future__ import annotations

import argparse
import re

import pytest

from hinoki import Hinoki, checkcli, doctorcli, http, routescli
from hinoki.doctorcli import Check
from hinoki.rules import Diagnostic, Phase, Severity, Status
from hinoki.terminal import Style

FANCY = Style(color=True, unicode=True)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def test_doctor_marks_each_status_and_shows_the_fix() -> None:
    checks = [
        Check("python", "ok", "3.13"),
        Check("manifest", "failed", "missing", "run `hinoki new`"),
        Check("extension", "warning", "not built"),
        Check("target", "skipped", "no target"),
    ]
    text = plain(doctorcli.render(checks, "text", FANCY))
    assert "Hinoki doctor" in text
    for glyph in ("✔", "✘", "▲", "\u2013"):
        assert glyph in text
    assert "→ run `hinoki new`" in text


def test_doctor_text_is_unchanged_without_colour() -> None:
    checks = [Check("python", "ok", "3.13")]
    expected = doctorcli.render(checks, "text")
    assert doctorcli.render(checks, "text", Style()) == expected
    assert "\x1b" not in expected
    assert expected.startswith("Hinoki doctor\nok      python")


def test_check_lists_findings_and_a_coloured_summary() -> None:
    found = [
        Diagnostic(
            "rule.one", Severity.ERROR, Phase.STATIC, "bad", "a.py:1", "why", fix="do x"
        ),
        Diagnostic(
            "rule.two",
            Severity.ERROR,
            Phase.STATIC,
            "unsure",
            "b.py:2",
            "why",
            Status.UNVERIFIABLE,
        ),
    ]
    text = plain(checkcli.render(found, 2, "text", FANCY))
    assert "✘  rule.one a.py:1" in text
    assert "fix: do x" in text
    assert "▲  rule.two b.py:2" in text
    assert "✘  2 rule(s): 1 violated, 1 unverifiable, 0 waived" in text
    assert "\x1b" in checkcli.render(found, 2, "text", FANCY)


def test_check_passes_with_a_green_summary() -> None:
    text = plain(checkcli.render([], 3, "text", FANCY))
    assert text.strip() == "✔  3 rule(s): 0 violated, 0 unverifiable, 0 waived"


def test_routes_colour_methods_and_keep_plain_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @http.get("/things")
    def things() -> list[str]:
        return []

    app = Hinoki(include=[things])

    monkeypatch.setattr(routescli, "build_app", lambda _target: app)
    args = argparse.Namespace(
        target="x:app", path="", method="", format="text", lanes=True
    )
    assert routescli.run_routes(args) == 0
    piped = capsys.readouterr().out
    assert "\x1b" not in piped
    assert piped.startswith("GET     /things")

    monkeypatch.setattr(Style, "detect", lambda *_a, **_k: FANCY)
    assert routescli.run_routes(args) == 0
    colored = capsys.readouterr().out
    assert "\x1b" in colored
    assert plain(colored).split()[:2] == ["GET", "/things"]
