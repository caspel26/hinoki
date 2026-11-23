"""Email templates: typed and untyped rendering, TOML sources, the errors they raise
and what those errors never contain."""

import math
import pickle
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import pytest

from hinoki import UNSET, Field, Schema, Unset, core, mail
from hinoki.computed import computed_field
from hinoki.fields import SecretStr
from hinoki.mail import TemplateError, TemplateReason
from hinoki.settings import Secret

SECRET = "sentinel-value-0xBEEF"


class Account(Schema):
    name: str
    trial_days: int


class Step(Schema):
    title: str
    url: str


class Welcome(Schema):
    user: Account
    steps: list[Step]


HTML = (
    "<h1>{{ user.name }}</h1>"
    "{% if user.trial_days %}<p>{{ user.trial_days }} days</p>"
    "{% else %}<p>active</p>{% endif %}"
    "<ul>{% for step in steps %}<li><a href='{{ step.url }}'>{{ step.title }}</a></li>"
    "{% endfor %}</ul>"
)


def welcome() -> mail.Template[Welcome]:
    return mail.Template(
        name="welcome", subject="Hi {{ user.name }}", html=HTML, context=Welcome
    )


def context(name: str = "Ada", days: int = 3) -> Welcome:
    return Welcome(
        user=Account(name=name, trial_days=days),
        steps=[Step(title="Start", url="https://example.com/start")],
    )


def refused(build: Any) -> TemplateError:
    with pytest.raises(TemplateError) as caught:
        build()
    return caught.value


def test_a_typed_template_renders_the_three_parts() -> None:
    rendered = welcome().render(context())

    assert rendered.subject == "Hi Ada"
    assert "<p>3 days</p>" in rendered.html
    assert "<a href='https://example.com/start'>Start</a>" in rendered.html
    assert "Start (https://example.com/start)" in rendered.text


def test_if_takes_the_else_branch_on_a_falsy_value() -> None:
    assert "<p>active</p>" in welcome().render(context(days=0)).html


def test_values_are_escaped_in_html_and_not_in_the_subject() -> None:
    rendered = welcome().render(context(name="<b>&\"'"))

    assert "<h1>&lt;b&gt;&amp;&quot;&#x27;</h1>" in rendered.html
    assert rendered.subject == "Hi <b>&\"'"


def test_a_line_break_in_a_subject_value_is_refused_without_the_value() -> None:
    error = refused(lambda: welcome().render(context(name=f"{SECRET}\r\nBcc: x")))

    assert error.reason is TemplateReason.HEADER_INJECTION
    assert error.field == "subject"
    assert SECRET not in str(error)
    assert "Bcc" not in str(error)


def test_an_explicit_text_part_replaces_the_derived_one() -> None:
    template = mail.Template(name="plain", subject="s", html="<p>x</p>", text="plain")

    assert template.render({}).text == "plain"


def test_a_dict_context_is_the_untyped_shortcut() -> None:
    template = mail.Template(
        name="greet", subject="Hi {{ who }}", html="<p>{{ n }} {{ ok }}</p>"
    )

    rendered = template.render({"who": "Ada", "n": 1.5, "ok": True})

    assert rendered.subject == "Hi Ada"
    assert "<p>1.5 true</p>" in rendered.html


def test_the_context_must_be_what_the_template_was_declared_with() -> None:
    with pytest.raises(TypeError, match="Welcome"):
        welcome().render({"user": {}})  # type: ignore[arg-type]
    untyped = mail.Template(name="u", subject="s", html="<p>x</p>")
    with pytest.raises(TypeError, match="mapping"):
        untyped.render([1])  # type: ignore[arg-type]


def test_an_untyped_template_reports_a_missing_variable_at_render() -> None:
    template = mail.Template(name="notice", subject="{{ day }}", html="<p>x</p>")

    error = refused(lambda: template.render({"other": SECRET}))

    assert error.reason is TemplateReason.MISSING_VARIABLE
    assert error.variable == "day"
    assert SECRET not in str(error)


def test_an_unknown_variable_is_refused_when_the_template_is_built() -> None:
    error = refused(
        lambda: mail.Template(
            name="welcome",
            subject="s",
            html="<p>{{ user.nickname }}</p>{{ user.name }}{{ user.trial_days }}"
            "{% for s in steps %}{{ s.title }}{{ s.url }}{% endfor %}",
            context=Welcome,
        )
    )

    assert error.reason is TemplateReason.UNKNOWN_VARIABLE
    assert error.variable == "user.nickname"
    assert error.kind is mail.EmailErrorKind.TEMPLATE
    assert str(error).startswith("template 'welcome': unknown variable 'user.nickname'")


def test_an_unused_field_is_refused() -> None:
    error = refused(
        lambda: mail.Template(
            name="w", subject="s", html="<p>{{ user.name }}</p>", context=Welcome
        )
    )

    assert {problem.reason for problem in error.problems} == {
        TemplateReason.UNUSED_FIELD
    }
    assert {problem.variable for problem in error.problems} == {
        "user.trial_days",
        "steps[].title",
        "steps[].url",
    }


def test_computed_fields_are_variables_of_the_context() -> None:
    class Person(Schema):
        first: str

        @computed_field
        def shout(self) -> str:
            return self.first.upper()

    template = mail.Template(
        name="p", subject="{{ shout }}", html="<p>{{ first }}</p>", context=Person
    )

    assert template.render(Person(first="ada")).subject == "ADA"


def test_secret_fields_and_values_are_never_rendered() -> None:
    class Login(Schema):
        token: SecretStr

    error = refused(
        lambda: mail.Template(name="l", subject="s", html="<p>x</p>", context=Login)
    )
    assert error.reason is TemplateReason.SECRET_FIELD

    template = mail.Template(name="d", subject="s", html="<p>{{ t }}</p>")
    for value in (SecretStr(SECRET), Secret(SECRET)):
        error = refused(lambda value=value: template.render({"t": value}))
        assert error.reason is TemplateReason.SECRET_VALUE
        assert error.variable == "t"
        assert SECRET not in str(error)


def test_an_unset_field_cannot_be_verified_statically() -> None:
    class WithUnset(Schema):
        name: str
        note: str | Unset = UNSET

    error = refused(
        lambda: mail.Template(
            name="u", subject="s", html="<p>{{ name }}{{ note }}</p>", context=WithUnset
        )
    )

    assert error.reason is TemplateReason.UNSUPPORTED_FIELD
    assert error.variable == "note"


def test_a_schema_that_overrides_dump_is_unsupported_at_any_depth() -> None:
    class Inner(Schema):
        name: str

        def dump(self, **options: Any) -> dict[str, Any]:
            return {"name": self.name.upper()}

    class Outer(Schema):
        inner: Inner

    nested = refused(
        lambda: mail.Template(
            name="o", subject="s", html="{{ inner.name }}", context=Outer
        )
    )
    root = refused(
        lambda: mail.Template(name="i", subject="s", html="{{ name }}", context=Inner)
    )

    assert nested.reason is TemplateReason.UNSUPPORTED_FIELD
    assert nested.variable == "inner"
    assert root.reason is TemplateReason.UNSUPPORTED_FIELD


def test_a_union_with_a_secret_is_a_secret_field() -> None:
    class Mixed(Schema):
        token: str | SecretStr

    error = refused(
        lambda: mail.Template(name="m", subject="s", html="{{ token }}", context=Mixed)
    )

    assert error.reason is TemplateReason.SECRET_FIELD


def test_a_computed_field_that_returns_a_secret_is_refused_at_render() -> None:
    class Inner(Schema):
        name: str

        @computed_field
        def code(self) -> str:
            return SecretStr(SECRET)  # type: ignore[return-value]

    class Outer(Schema):
        items: list[Inner]

    template = mail.Template(
        name="c",
        subject="s",
        html="{% for i in items %}{{ i.name }}{{ i.code }}{% endfor %}",
        context=Outer,
    )

    error = refused(lambda: template.render(Outer(items=[Inner(name="Ada")])))

    assert error.reason is TemplateReason.SECRET_VALUE
    assert error.variable == "items.code"
    assert SECRET not in str(error) and "**********" not in str(error)


def test_integers_of_any_size_render_and_keys_are_cleaned_in_errors() -> None:
    template = mail.Template(name="n", subject="{{ n }}", html="<p>{{ n }}</p>")

    assert template.render({"n": 2**70}).subject == str(2**70)
    hostile: Any = {"n": 1, "a\nb": {"x": object()}}
    error = refused(lambda: template.render(hostile))
    assert error.reason is TemplateReason.UNSUPPORTED_VALUE
    assert error.variable == "a?b.x"


def test_html_places_escaping_cannot_protect_are_refused() -> None:
    link = mail.Template(name="l", subject="s", html='<a href="{{ u }}">x</a>')

    assert (
        link.render({"u": "https://example.com/?a=1"}).text
        == "x (https://example.com/?a=1)"
    )
    error = refused(lambda: link.render({"u": "javascript:alert(1)"}))
    assert (error.reason, error.variable) == (TemplateReason.UNSAFE_URL, "u")
    assert "alert" not in str(error)
    error = refused(
        lambda: mail.Template(name="s", subject="s", html="<script>{{ u }}</script>")
    )
    assert error.reason is TemplateReason.UNSAFE_CONTEXT
    assert (error.line, error.column) == (1, 12)


def test_a_file_description_is_kept_by_load(tmp_path: Path) -> None:
    path = tmp_path / "d.toml"
    path.write_text('description = "Sent once"\nsubject = "s"\ntemplate = "<p>x</p>"\n')
    template = mail.Template.file(path)

    assert template.description is None
    template.load()
    assert template.description == "Sent once"


@pytest.mark.parametrize("annotation", [bytes, Any, dict[str, str]])
def test_fields_without_a_text_form_are_unsupported(annotation: object) -> None:
    class Odd(Schema):
        value: annotation  # type: ignore[valid-type]

    error = refused(
        lambda: mail.Template(name="o", subject="s", html="<p>x</p>", context=Odd)
    )

    assert error.reason is TemplateReason.UNSUPPORTED_FIELD
    assert error.variable == "value"


class Chain(Schema):
    child: "Chain | None" = None
    label: str = ""


def test_a_recursive_schema_is_too_deep() -> None:
    error = refused(
        lambda: mail.Template(name="n", subject="s", html="<p>x</p>", context=Chain)
    )

    assert error.reason is TemplateReason.TOO_DEEP


def test_context_values_without_a_text_form_are_refused_by_path() -> None:
    template = mail.Template(name="v", subject="s", html="<p>{{ a.b }}</p>")

    for bad in (math.nan, object(), {1: "x"}, b"raw"):
        error = refused(lambda bad=bad: template.render({"a": {"b": bad}}))
        assert error.reason is TemplateReason.UNSUPPORTED_VALUE
        assert error.variable == "a.b"


def test_dates_decimals_enums_and_urls_render_as_text() -> None:
    class Color(Enum):
        RED = "red"

    class Facts(Schema):
        when: datetime
        price: Decimal
        color: Color
        link: Annotated[str, Field(format="url")]

    template = mail.Template(
        name="facts",
        subject="{{ color }}",
        html="<p>{{ when }} {{ price }} {{ link }}</p>",
        context=Facts,
    )
    facts = Facts(
        when=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        price=Decimal("12.50"),
        color=Color.RED,
        link="https://example.com/x",
    )

    rendered = template.render(facts)

    assert rendered.subject == "red"
    assert "2030-01-02T03:04:05" in rendered.html
    assert "12.50" in rendered.html


def test_from_toml_reads_every_key() -> None:
    source = """
description = "d"
subject = "Hi"
template = "<p>x</p>"
text = "plain"
"""

    template = mail.Template.from_toml(source, name="t")

    assert template.description == "d"
    assert template.render({}).text == "plain"


@pytest.mark.parametrize(
    ("source", "variable"),
    [
        ('subject = "s"\ntemplate = "t"\nextra = 1', "extra"),
        ('subject = 1\ntemplate = "t"', "subject"),
        ('template = "t"', "subject"),
        ('subject = "s"', "template"),
    ],
)
def test_from_toml_refuses_unknown_keys_wrong_types_and_missing_keys(
    source: str, variable: str
) -> None:
    error = refused(lambda: mail.Template.from_toml(source, name="t"))

    assert error.reason is TemplateReason.INVALID_FILE
    assert error.variable == variable


def test_toml_that_does_not_parse_reports_a_position_only() -> None:
    error = refused(
        lambda: mail.Template.from_toml(f'subject = "{SECRET}\nx', name="t")
    )

    assert error.reason is TemplateReason.INVALID_FILE
    assert error.line is not None
    assert SECRET not in str(error)


def test_a_template_error_survives_pickling_and_has_a_stable_repr() -> None:
    error = refused(lambda: mail.Template(name="n", subject="{{", html="x"))

    copy = pickle.loads(pickle.dumps(error))

    assert (copy.template, copy.problems, str(copy)) == (
        error.template,
        error.problems,
        str(error),
    )
    assert repr(error) == f"TemplateError('n', reason={error.reason.value!r})"


def test_a_toml_source_over_the_file_limit_is_too_large() -> None:
    source = f'subject = "s"\ntemplate = "{"x" * mail.MAX_FILE_BYTES}"'

    error = refused(lambda: mail.Template.from_toml(source, name="t"))

    assert error.reason is TemplateReason.TOO_LARGE


def test_a_plain_union_is_text_only_when_every_variant_is() -> None:
    class Mixed(Schema):
        id: int | str

    class Raw(Schema):
        id: int | bytes

    ok = mail.Template(name="m", subject="{{ id }}", html="x", context=Mixed)
    error = refused(lambda: mail.Template(name="r", subject="s", html="x", context=Raw))

    assert ok.render(Mixed(id=7)).subject == "7"
    assert error.reason is TemplateReason.UNSUPPORTED_FIELD


def test_a_template_error_needs_a_problem() -> None:
    with pytest.raises(ValueError, match="at least one problem"):
        TemplateError(template="t", problems=[])


def test_more_problems_than_the_cap_are_reported_as_truncated() -> None:
    html = "".join(f"{{{{ v{i} }}}}" for i in range(40))
    error = refused(
        lambda: mail.Template(name="many", subject="s", html=html, context=Welcome)
    )

    assert error.truncated
    assert len(error.problems) <= 20
    assert str(error).endswith("more problems)")


def test_the_reasons_are_the_ones_the_engine_reports() -> None:
    assert [reason.value for reason in TemplateReason] == core.template_reasons()
    assert dict(core.template_limits()) == mail.TEMPLATE_LIMITS


def test_names_are_checked_by_the_engine() -> None:
    error = refused(lambda: mail.Template(name="Bad Name", subject="s", html="x"))

    assert error.reason is TemplateReason.INVALID_NAME
    assert "Bad Name" not in str(error)


def test_a_template_file_is_read_by_the_mailer_not_when_it_is_declared(
    tmp_path: Path,
) -> None:
    template = mail.Template.file(tmp_path / "later.toml")

    assert not template.loaded
    assert template.path == tmp_path / "later.toml"
