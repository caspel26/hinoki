"""``hinoki mail``: check and preview email templates, and the HNK-MAIL-001 rule."""

import io
import json
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated

import pytest

from hinoki import Field, Hinoki, Schema, mail
from hinoki.__main__ import main
from hinoki.mailcli import sample_of
from hinoki.rules import EmailTemplatesMatch, RuleSet
from hinoki.settings import AppSettings, EmailBackend, EmailSettings
from hinoki.testing import ASGIClient

APP = "tests.mailclitest:make_app"
BARE = "tests.mailclitest:bare_app"
FILES = {"directory": Path()}


class Level(Enum):
    HIGH = "high"


class Person(Schema):
    name: Annotated[str, Field(examples=("Ada",))]
    nick: str | None = None


class Team(Schema):
    lead: Person
    members: Annotated[list[Person], Field(max_length=5)]


class Unexampled(Schema):
    name: str


class MailSettings(AppSettings):
    email: EmailSettings


def settings() -> MailSettings:
    return MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))


def make_app() -> Hinoki:
    directory = FILES["directory"]
    return Hinoki(
        settings=settings(),
        email_templates=[
            mail.Template(
                name="person",
                subject="Hi {{ name }}",
                html="<p>{{ name }}</p><p>{{ nick }}</p>",
                context=Person,
            ),
            mail.Template.file(directory / "team.toml", context=Team),
            mail.Template(name="free", subject="{{ a }}", html="<p>x</p>"),
            mail.Template(
                name="bare", subject="s", html="<p>{{ name }}</p>", context=Unexampled
            ),
        ],
    )


def bare_app() -> Hinoki:
    return Hinoki()


def rules() -> RuleSet:
    return RuleSet(EmailTemplatesMatch())


TEAM = """
subject = "Team of {{ lead.name }}"
template = '''
<p>{{ lead.name }} {{ lead.nick }}</p>
{% for member in members %}<li>{{ member.name }} {{ member.nick }}</li>{% endfor %}
'''
"""


@pytest.fixture(autouse=True)
def team_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "team.toml"
    path.write_text(TEAM)
    monkeypatch.setitem(FILES, "directory", tmp_path)
    return path


def run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    code = main(list(arguments))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_preview_renders_the_examples_of_the_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, "mail", "preview", "person", APP)

    assert code == 0
    assert out.startswith("Subject: Hi Ada\n----- text -----\n")
    assert "----- html -----\n<p>Ada</p>" in out


def test_preview_builds_nested_samples_from_the_structure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, "mail", "preview", "team", APP, "--part", "html")

    assert code == 0
    assert out.count("<li>Ada ") == 1


def test_samples_take_python_examples_and_respect_list_lengths() -> None:
    class Step(Schema):
        title: Annotated[str, Field(examples=("Go",))]

    class Plan(Schema):
        level: Annotated[Level, Field(examples=(Level.HIGH,))]
        day: Annotated[date, Field(examples=(date(2026, 1, 1),))]
        price: Annotated[Decimal, Field(examples=(Decimal("9.99"),))]
        steps: Annotated[list[Step], Field(min_length=2)]

    sample = sample_of(Plan)

    assert Plan.validate(sample).steps == [Step(title="Go"), Step(title="Go")]


def test_preview_part_and_json_formats(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(
        capsys, "mail", "preview", "person", APP, "--format", "json", "--part", "html"
    )
    assert code == 2
    assert "--part cannot be combined" in err
    _, subject, _ = run(capsys, "mail", "preview", "person", APP, "--part", "subject")
    _, text, _ = run(capsys, "mail", "preview", "person", APP, "--part", "text")
    _, html, _ = run(capsys, "mail", "preview", "person", APP, "--part", "html")
    _, body, _ = run(capsys, "mail", "preview", "person", APP, "--format", "json")

    assert subject == "Hi Ada\n"
    assert text.startswith("Ada")
    assert html.startswith("<p>Ada</p>")
    assert json.loads(body) == {
        "subject": "Hi Ada",
        "text": "Ada",
        "html": "<p>Ada</p><p></p>",
    }


def test_preview_reads_a_context_file_or_standard_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"name": "Grace <g>"}))

    _, from_file, _ = run(
        capsys,
        *("mail", "preview", "person", APP, "--context", str(path), "--part", "html"),
    )
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"name": "Eve"}')))
    _, from_stdin, _ = run(
        capsys,
        *("mail", "preview", "person", APP, "--context", "-", "--part", "subject"),
    )

    assert "<p>Grace &lt;g&gt;</p>" in from_file
    assert from_stdin == "Hi Eve\n"


def test_preview_takes_a_dict_for_an_untyped_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "context.json"
    path.write_text('{"a": 1}')

    code, out, _ = run(
        capsys,
        "mail",
        "preview",
        "free",
        APP,
        "--context",
        str(path),
        "--part",
        "subject",
    )

    assert (code, out) == (0, "1\n")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["preview", "nothing"], "unknown template 'nothing'"),
        (["preview", "free"], "pass --context"),
        (["preview", "bare"], "field 'name' has no example"),
    ],
)
def test_preview_refuses_what_it_cannot_build(
    capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    code, out, err = run(capsys, "mail", *arguments, APP)

    assert (code, out) == (2, "")
    assert message in err


def test_preview_refuses_bad_contexts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"name": 5}')
    huge = tmp_path / "huge.json"
    huge.write_text(" " * (1024 * 1024 + 1))
    listing = tmp_path / "list.json"
    listing.write_text("[]")
    cases = [
        ("person", broken, "not valid JSON"),
        ("person", wrong, "does not match"),
        ("person", huge, "larger than"),
        ("person", tmp_path / "absent.json", "cannot read the context"),
        ("free", listing, "must be a JSON object"),
    ]

    for name, path, message in cases:
        code, _, err = run(capsys, "mail", "preview", name, APP, "--context", str(path))
        assert code == 2, name
        assert message in err


def test_preview_reports_a_template_error_with_status_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "context.json"
    path.write_text("{}")

    code, _, err = run(capsys, "mail", "preview", "free", APP, "--context", str(path))

    assert code == 1
    assert err.startswith("error: template 'free': missing variable 'a'")


def test_an_application_without_an_email_module_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for action in (["check"], ["preview", "x"]):
        code, _, err = run(capsys, "mail", *action, BARE)
        assert code == 2
        assert "the application has no email module" in err


def test_check_lists_every_template(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "mail", "check", APP)

    assert code == 0
    lines = out.splitlines()
    assert [line.split()[:2] for line in lines] == [
        ["ok", "person"],
        ["ok", "team"],
        ["ok", "free"],
        ["ok", "bare"],
    ]
    assert lines[0].endswith("(inline)")
    assert lines[1].endswith("team.toml)")


def test_check_reports_each_problem_and_exits_one(
    team_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    team_file.write_text(TEAM.replace("lead.name", "lead.title", 1))

    code, out, _ = run(capsys, "mail", "check", APP)

    assert code == 1
    assert "template 'team': unknown variable 'lead.title' in subject" in out
    assert "ok  person" in out


def test_check_json_is_machine_readable(
    team_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    team_file.write_text("subject = 1")

    code, out, _ = run(capsys, "mail", "check", APP, "--format", "json")

    rows = {row["name"]: row for row in json.loads(out)["templates"]}
    assert code == 1
    assert rows["person"]["ok"] is True
    assert rows["team"]["ok"] is False
    assert "invalid template file" in rows["team"]["error"]


def test_a_missing_file_is_reported_without_its_path(
    team_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    team_file.unlink()

    code, out, _ = run(capsys, "mail", "check", APP)

    assert code == 1
    assert "template file cannot be read" in out
    assert str(team_file.parent) not in out.splitlines()[1]


def test_the_rule_is_effectful_and_silent_for_correct_templates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = ["check", APP, "--rules", "tests.mailclitest:rules"]

    assert main(arguments) == 1
    assert "unverifiable" in capsys.readouterr().out
    assert main([*arguments, "--allow-effects"]) == 0
    assert "HNK-MAIL-001" not in capsys.readouterr().out


def test_the_rule_reports_each_broken_template(
    team_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    team_file.write_text(TEAM.replace("lead.name", "lead.title", 1))

    code = main(["check", APP, "--rules", "tests.mailclitest:rules", "--allow-effects"])

    out = capsys.readouterr().out
    assert code == 1
    assert "HNK-MAIL-001" in out
    assert "unknown variable 'lead.title'" in out


def test_the_rule_ignores_an_application_without_templates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        ["check", BARE, "--rules", "tests.mailclitest:rules", "--allow-effects"]
    )

    assert code == 0


async def test_the_application_itself_starts_with_the_same_templates() -> None:
    async with ASGIClient(make_app()):
        pass
