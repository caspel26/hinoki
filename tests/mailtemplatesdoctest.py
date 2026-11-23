"""The email templates page tells the truth: its code is the example's code and its
outputs are what the commands print today.

Every fenced block whose first line is ``# mailroom/...`` or ``# mailroom_tests/...``
is a contiguous piece of that file under ``examples/mailroom``. Every
``<!-- output: KEY -->`` marker is followed by a block that equals what running the
example (or a broken copy of it) produces now.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mailroom.messages import Welcome
from mailroom.notices import notice_template

from hinoki import mail

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "mailroom"
PAGE = ROOT / "docs" / "email-templates.md"
BLOCK = re.compile(r"^```[a-z]*\n(.*?)^```$", re.MULTILINE | re.DOTALL)
OUTPUT = re.compile(
    r"<!-- output: ([a-z-]+) -->\n```[a-z]*\n(.*?)^```$", re.MULTILINE | re.DOTALL
)
SAMPLE = "mailroom/samples/reset_password.json"
ENVIRONMENT = {
    **os.environ,
    "MAILROOM_EMAIL__BACKEND": "memory",
    "MAILROOM_EMAIL__DEFAULT_SENDER": "noreply@example.com",
    "NO_COLOR": "1",
}


def page() -> str:
    return PAGE.read_text()


def command(arguments: list[str], cwd: Path) -> str:
    """What ``hinoki`` prints on standard output, as a user would see it."""
    done = subprocess.run(
        [sys.executable, "-m", "hinoki", *arguments],
        cwd=cwd,
        env=ENVIRONMENT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return done.stdout


def copy_of_example(tmp_path: Path) -> Path:
    target = tmp_path / "mailroom-copy"
    shutil.copytree(
        EXAMPLE, target, ignore=shutil.ignore_patterns("__pycache__", "mailroom_tests")
    )
    return target


def broken_welcome(directory: Path, old: str, new: str) -> Path:
    path = directory / "mailroom" / "templates" / "welcome.toml"
    source = path.read_text()
    assert old in source
    path.write_text(source.replace(old, new))
    return path


def refusal(path: Path) -> str:
    template = mail.Template.file(path, context=Welcome)
    with pytest.raises(mail.TemplateError) as caught:
        template.load()
    return str(caught.value)


def without_list(source: str) -> str:
    start, end = source.index("<ol>"), source.index("</ol>\n") + len("</ol>\n")
    return source[:start] + source[end:]


def nickname_copy(tmp_path: Path) -> Path:
    directory = copy_of_example(tmp_path)
    broken_welcome(directory, "{{ user.name }}</h1>", "{{ user.nickname }}</h1>")
    return directory


def reset_text(_: Path) -> str:
    arguments = ["mail", "preview", "reset_password", "--context", SAMPLE]
    return command([*arguments, "--part", "text"], EXAMPLE)


def unknown_variable(tmp_path: Path) -> str:
    directory = nickname_copy(tmp_path)
    return refusal(directory / "mailroom" / "templates" / "welcome.toml")


def unused_field(tmp_path: Path) -> str:
    directory = copy_of_example(tmp_path)
    path = directory / "mailroom" / "templates" / "welcome.toml"
    path.write_text(without_list(path.read_text()))
    return refusal(path)


def missing_variable(_: Path) -> str:
    with pytest.raises(mail.TemplateError) as caught:
        notice_template("maintenance").render({"day": "Friday"})
    return str(caught.value)


PRODUCERS = {
    "reset-text": reset_text,
    "unknown-variable": unknown_variable,
    "unused-field": unused_field,
    "mail-check": lambda tmp_path: command(["mail", "check"], nickname_copy(tmp_path)),
    "mail-check-ok": lambda _: command(["mail", "check"], EXAMPLE),
    "welcome-preview": lambda _: command(["mail", "preview", "welcome"], EXAMPLE),
    "missing-variable": missing_variable,
}


def produce(key: str, tmp_path: Path) -> str:
    return PRODUCERS[key](tmp_path)


KEYS = [
    "reset-text",
    "unknown-variable",
    "unused-field",
    "mail-check",
    "mail-check-ok",
    "welcome-preview",
    "missing-variable",
]


def test_every_example_block_is_a_piece_of_the_example_file() -> None:
    checked = 0
    for match in BLOCK.finditer(page()):
        first, _, body = match.group(1).partition("\n")
        found = re.fullmatch(r"# (mailroom(?:_tests)?/\S+)", first)
        if found is None:
            continue
        source = (EXAMPLE / found.group(1)).read_text()
        assert body in source, f"{found.group(1)} no longer contains this block"
        checked += 1
    assert checked >= 6


def test_the_page_has_exactly_the_outputs_the_test_can_produce() -> None:
    assert [key for key, _ in OUTPUT.findall(page())] == KEYS


@pytest.mark.parametrize("key", KEYS)
def test_each_documented_output_is_what_the_command_prints(
    key: str, tmp_path: Path
) -> None:
    documented = dict(OUTPUT.findall(page()))[key]

    assert documented.rstrip("\n") == produce(key, tmp_path).rstrip("\n")
