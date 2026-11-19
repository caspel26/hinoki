from __future__ import annotations

import shutil
import subprocess

import pytest

from hinoki import completioncli
from hinoki.__main__ import main, parser
from hinoki.clihelp import command_help, grouped
from hinoki.reportui import run_timed
from hinoki.terminal import Style


def test_a_mistyped_command_gets_a_suggestion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["chek"])
    assert caught.value.code == 2
    assert "did you mean 'check'?" in capsys.readouterr().err


def test_a_mistyped_subcommand_gets_a_suggestion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["migrations", "aply"])
    assert "did you mean 'apply'?" in capsys.readouterr().err


def test_an_unrelated_word_gets_no_suggestion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["zzzzzz"])
    assert "did you mean" not in capsys.readouterr().err


def test_help_groups_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    commands = command_help(parser())
    assert all(f"  {name}" in out for name in commands)
    assert dict(grouped(commands))["Other"] == []
    assert out.index("Start") < out.index("Inspect") < out.index("Data")


def test_help_of_a_command_is_still_argparse_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["routes", "--help"])
    assert "--lanes" in capsys.readouterr().out


@pytest.mark.parametrize("shell", completioncli.SHELLS)
def test_completion_scripts_cover_commands_and_options(
    shell: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["completion", shell]) == 0
    script = capsys.readouterr().out
    for word in ("migrations", "plan", "lanes", "allow-destructive"):
        assert word in script


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_completion_follows_the_typed_command() -> None:
    script = completioncli.generate("bash", parser())
    probe = (
        "COMP_WORDS=(hinoki --format json migrations ap); COMP_CWORD=4; _hinoki; "
        'echo "${COMPREPLY[*]}"'
    )
    bash = shutil.which("bash") or "bash"
    done = subprocess.run(
        [bash, "-c", f"{script}\n{probe}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "apply"


def test_timed_action_is_unchanged_without_an_animated_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def action() -> int:
        print("plain output")
        return 0

    assert run_timed("applying", "applied", action) == 0
    assert capsys.readouterr().out == "plain output\n"


def test_timed_action_prints_output_then_the_elapsed_time(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fancy = Style(color=True, unicode=True, animated=True)
    monkeypatch.setattr(Style, "detect", lambda *_a, **_k: fancy)

    def action() -> int:
        print("app: 0001_initial")
        return 1

    assert run_timed("applying", "applied", action) == 1
    out = capsys.readouterr().out
    assert "app: 0001_initial" in out
    assert "applied" in out
    assert out.index("0001_initial") < out.index("applied")
