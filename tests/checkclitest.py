"""Waivers and the ``check`` command."""

import datetime
import json
from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.rules import Diagnostic, Phase, Severity, Status
from hinoki.waivers import (
    EXPIRED,
    STALE,
    WaiverError,
    apply_waivers,
    load_waivers,
)

TODAY = datetime.date(2026, 11, 13)
APP = "tests.projects.rulesapp:create_app"
RULES = "tests.projects.rulesapp:rules"


def finding(subject: str = "pkg.health", rule: str = "HNK-ACC-001") -> Diagnostic:
    return Diagnostic(
        rule,
        Severity.ERROR,
        Phase.STARTUP,
        "m",
        "f.py:1",
        "e",
        Status.VIOLATED,
        subject,
    )


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


GOOD = """
[[waiver]]
rule = "HNK-ACC-001"
target = "pkg.health"
reason = "liveness probe"
owner = "platform"
expires = 2027-01-31
"""


def test_a_waiver_marks_matching_findings_and_keeps_them_visible(
    tmp_path: Path,
) -> None:
    waivers = load_waivers(write(tmp_path / "w.toml", GOOD))
    result = apply_waivers([finding(), finding("pkg.other")], waivers, TODAY)
    assert [(d.subject, d.status) for d in result] == [
        ("pkg.health", Status.WAIVED),
        ("pkg.other", Status.VIOLATED),
    ]
    assert result[0].waiver == "liveness probe (platform, until 2027-01-31)"


def test_expired_and_unused_waivers_are_reported(tmp_path: Path) -> None:
    waivers = load_waivers(write(tmp_path / "w.toml", GOOD))
    late = apply_waivers([finding()], waivers, datetime.date(2027, 2, 1))
    assert late[0].status is Status.VIOLATED  # no longer waived
    assert late[1].rule_id == EXPIRED
    unused = apply_waivers([], waivers, TODAY)
    assert [d.rule_id for d in unused] == [STALE]
    assert apply_waivers([finding(rule="OTHER")], waivers, TODAY)[0].status is (
        Status.VIOLATED
    )


def entry(**changes: str) -> str:
    keys = {"rule": "A", "target": "x", "reason": "r", "owner": "o", **changes}
    return "[[waiver]]\n" + "".join(
        f'{key} = "{value}"\n' for key, value in keys.items() if value
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (entry(reason=""), "missing reason"),
        (entry(reason="  "), "missing reason"),
        (entry(target="*"), "covers everything"),
        (entry(extra="1"), "unknown keys"),
        (entry(expires="soon"), "expires"),
        ("[[waiver", "waiver"),
        ('waiver = "x"', "list of tables"),
    ],
)
def test_invalid_waiver_files_fail_instead_of_being_guessed(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(WaiverError, match=message):
        load_waivers(write(tmp_path / "w.toml", text))


def test_an_absent_waiver_file_means_no_waivers(tmp_path: Path) -> None:
    assert load_waivers(tmp_path / "none.toml") == []


def test_check_reports_violations_with_a_failing_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = main(
        ["check", "--app", APP, "--rules", RULES, "--waivers", str(tmp_path / "n")]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "HNK-ACC-001" in out
    assert "HNK-TXN-001" in out
    assert "violated" in out.splitlines()[-1]


def test_check_json_is_versioned_and_waivers_make_it_pass(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    waivers = write(
        tmp_path / "w.toml",
        "".join(
            f'[[waiver]]\nrule="{rule}"\ntarget="tests.projects.rulesapp.*"\n'
            'reason="fixture"\nowner="tests"\n'
            for rule in ("HNK-ACC-001", "HNK-TXN-001")
        ),
    )
    code = main(
        [
            "check",
            "--app",
            APP,
            "--rules",
            RULES,
            "--waivers",
            str(waivers),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["schema_version"] == 1
    assert report["summary"]["waived"] == 3 and report["summary"]["failing"] == 0
    assert {d["status"] for d in report["diagnostics"]} == {"waived"}


def test_check_without_rules_passes_and_says_zero(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["check", "--app", APP, "--waivers", str(tmp_path / "n")]) == 0
    assert "0 rule(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--app", "tests.projects.nope:create_app"],
        ["--app", "tests.projects.rulesapp:missing"],
        ["--app", "tests.projects.rulesapp"],
        ["--app", "tests.projects.rulesapp:rules"],
        ["--app", APP, "--rules", "tests.projects.rulesapp:health"],
    ],
)
def test_check_that_cannot_run_exits_2(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", *arguments]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_a_broken_waiver_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write(tmp_path / "w.toml", "[[waiver")
    assert main(["check", "--app", APP, "--waivers", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
