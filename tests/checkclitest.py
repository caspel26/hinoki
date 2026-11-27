"""Waivers and the ``check`` command."""

import datetime
import json
import sys
from pathlib import Path

import pytest

from hinoki import scenario
from hinoki.__main__ import main
from hinoki.cli.check import load_object
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
        (entry(target="*"), "not bounded"),
        (entry(extra="1"), "unknown keys"),
        (entry(expires="soon"), "expires"),
        ("[[waiver", "waiver"),
        ('waiver = "x"', "list of tables"),
        ("waiver = [1]", "list of tables"),
        ('waiver = ["ab"]', "list of tables"),
    ],
)
def test_invalid_waiver_files_fail_instead_of_being_guessed(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(WaiverError, match=message):
        load_waivers(write(tmp_path / "w.toml", text))


def test_an_unreadable_waiver_file_is_an_error_not_an_absence(tmp_path: Path) -> None:
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(WaiverError, match=r"binary\.toml: cannot read"):
        load_waivers(binary)
    folder = tmp_path / "folder.toml"
    folder.mkdir()
    dangling = tmp_path / "dangling.toml"
    dangling.symlink_to(tmp_path / "gone.toml")
    for path in (folder, dangling):
        with pytest.raises(WaiverError, match="not a regular file"):
            load_waivers(path)


def project_with_waivers(tmp_path: Path, text: str) -> Path:
    """A project root with ``hinoki.toml`` and ``hinoki-rules.toml``, and a
    subdirectory holding a stray, invalid ``hinoki-rules.toml``."""
    write(tmp_path / "hinoki.toml", '[project]\napplication = "shop.main:app"\n')
    write(tmp_path / "hinoki-rules.toml", text)
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub / "hinoki-rules.toml", "[[waiver")
    return sub


def test_check_reads_the_waivers_of_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = "".join(
        f'[[waiver]]\nrule="{rule}"\ntarget="tests.projects.rulesapp.*"\n'
        'reason="r"\nowner="o"\n'
        for rule in ("HNK-ACC-001", "HNK-TXN-001")
    )
    monkeypatch.chdir(project_with_waivers(tmp_path, every))
    assert main(["check", APP, "--rules", RULES]) == 0


def test_check_refuses_a_waiver_path_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "hinoki-rules.toml").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["check", APP, "--rules", RULES]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "not a regular file" in err
    assert "Traceback" not in err


def test_an_absent_waiver_file_means_no_waivers(tmp_path: Path) -> None:
    assert load_waivers(tmp_path / "none.toml") == []


def test_check_reports_violations_with_a_failing_exit_code(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)  # no hinoki-rules.toml here
    code = main(["check", APP, "--rules", RULES])
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
    assert report["schema_version"] == 2
    assert report["summary"]["waived"] == 3 and report["summary"]["failing"] == 0
    assert {d["status"] for d in report["diagnostics"]} == {"waived"}


def test_text_keeps_waivers_with_their_finding_and_counts_warnings_apart(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    waivers = write(
        tmp_path / "w.toml",
        GOOD.replace("pkg.health", "tests.projects.rulesapp.health")
        + '[[waiver]]\nrule="HNK-API-001"\ntarget="tests.projects.none"\n'
        'reason="unused"\nowner="tests"\n',
    )
    main(["check", APP, "--rules", RULES, "--waivers", str(waivers)])
    lines = capsys.readouterr().out.splitlines()
    waived = next(i for i, line in enumerate(lines) if "[waived]" in line)
    assert lines[waived + 2].startswith("    waived: liveness probe")
    assert any(f"{STALE} [warning]" in line for line in lines)
    assert lines[-1] == "2 rule(s): 2 violated, 0 unverifiable, 1 waived, 1 warning(s)"
    main(
        ["check", APP, "--rules", RULES, "--waivers", str(waivers), "--format", "json"]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 2
    assert (report["summary"]["violated"], report["summary"]["warning"]) == (2, 1)
    statuses = {d["rule"]: d["status"] for d in report["diagnostics"]}
    assert statuses[STALE] == "warning"


def test_check_without_rules_passes_and_says_zero(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["check", APP]) == 0
    captured = capsys.readouterr()
    assert "0 rule(s)" in captured.out
    assert "warning: no rules were run" in captured.err
    assert "Hinoki(rules=" in captured.err


def test_check_with_rules_prints_no_warning(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    main(["check", APP, "--rules", RULES])
    assert "warning" not in capsys.readouterr().err


def test_target_imports_from_the_current_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "sampleproject"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "api.py").write_text("answer = 42\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [item for item in sys.path if item != ""])
    assert load_object("sampleproject.api:answer") == 42


@pytest.mark.parametrize(
    "arguments",
    [
        ["tests.projects.nope:create_app"],
        ["tests.projects.rulesapp:missing"],
        ["tests.projects.rulesapp"],
        ["tests.projects.rulesapp:rules"],
        [APP, "--rules", "tests.projects.rulesapp:health"],
    ],
)
def test_check_that_cannot_run_exits_2(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", *arguments]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_mcp_help_names_the_methods_that_read_scenarios_allow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["mcp", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "GET only" not in text
    assert "read = " + ", ".join(sorted(scenario.SAFE)) in text


def test_a_broken_waiver_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = write(tmp_path / "w.toml", "[[waiver")
    assert main(["check", APP, "--waivers", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["check", "mcp"])
def test_waivers_help_names_the_real_default(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([command, "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "Hinoki(waivers=...), else hinoki-rules.toml at the project root" in shown


def project_with_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A manifest project whose rules need its models imported."""
    package = tmp_path / "checkmodelsproj"
    package.mkdir()
    write(package / "__init__.py", "")
    write(
        package / "models.py",
        "from hinoki.orm import Model, field\n\n\nclass Note(Model):\n"
        "    id: int = field(primary_key=True)\n"
        "    title: str = field(max_length=40)\n",
    )
    write(
        package / "main.py",
        "from hinoki import Hinoki\n"
        "from hinoki.rules import Models, RuleSet\n\n"
        "app = Hinoki(rules=RuleSet(Models.require_explicit_table("
        'match="checkmodelsproj.*")))\n',
    )
    write(
        tmp_path / "hinoki.toml",
        '[project]\napplication = "checkmodelsproj.main:app"\n\n'
        '[[apps]]\nname = "checkmodelsproj"\nmodels = "checkmodelsproj.models"\n'
        'migrations = "checkmodelsproj.migrations"\nrequires = []\n',
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_check_imports_the_models_named_in_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_with_models(tmp_path, monkeypatch)
    try:
        assert main(["check"]) == 1
    finally:
        for name in [m for m in sys.modules if m.startswith("checkmodelsproj")]:
            del sys.modules[name]
    out = capsys.readouterr().out
    assert "derived table name" in out
    assert "unverifiable" not in out.replace("0 unverifiable", "")
