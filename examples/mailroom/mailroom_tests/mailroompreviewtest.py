"""The shipped templates pass the startup check and preview from their examples."""

from pathlib import Path

import pytest

from hinoki.__main__ import main

SAMPLE = Path(__file__).parents[1] / "mailroom" / "samples" / "reset_password.json"


def test_the_shipped_templates_pass_the_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["mail", "check", "mailroom.main:app"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[:2] for line in lines] == [
        ["ok", "welcome"],
        ["ok", "reset_password"],
    ]


@pytest.mark.parametrize("name", ["welcome", "reset_password"])
def test_each_template_previews_from_its_examples(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["mail", "preview", name, "mailroom.main:app"]) == 0

    assert capsys.readouterr().out.startswith("Subject: ")


def test_the_sample_context_renders_and_escapes_html(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = ["mail", "preview", "reset_password", "mailroom.main:app"]
    code = main([*arguments, "--context", str(SAMPLE), "--part", "html"])

    assert code == 0
    assert "<li>Phone &lt;work&gt;</li>" in capsys.readouterr().out
