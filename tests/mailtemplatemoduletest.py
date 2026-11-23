"""Email templates in a running application: registration, the worker's startup
check, sending a rendered message and the counters of a refused render."""

from pathlib import Path

import pytest

from hinoki import Hinoki, Schema, http, mail, settings
from hinoki.errors import RegistrationError
from hinoki.mail import TemplateError, TemplateReason
from hinoki.settings import EmailBackend, EmailSettings
from hinoki.testing import ASGIClient


class MailSettings(settings.AppSettings):
    email: EmailSettings


class Greeting(Schema):
    name: str


def memory_settings() -> MailSettings:
    return MailSettings(
        email=EmailSettings(
            backend=EmailBackend.MEMORY, default_sender="noreply@example.com"
        )
    )


def write(
    path: Path, subject: str = "Hi {{ name }}", body: str = "<p>{{ name }}</p>"
) -> Path:
    path.write_text(f"subject = '{subject}'\ntemplate = '{body}'\n")
    return path


def email_module(app: Hinoki) -> mail.EmailModule:
    return next(m for m in app.modules if isinstance(m, mail.EmailModule))


INLINE = mail.Template(
    name="inline", subject="Hi {{ name }}", html="<p>{{ name }}</p>", context=Greeting
)


@http.post("/greet")
async def greet() -> dict[str, str]:
    sent = await mail.send(
        INLINE.message(Greeting(name="Ada"), to=("ada@example.com",))
    )
    return {"id": sent.message_id}


async def test_a_registered_template_is_sent_with_text_and_html() -> None:
    app = Hinoki(settings=memory_settings(), email_templates=[INLINE])
    app.include(greet)
    async with ASGIClient(app) as client:
        assert (await client.post("/greet")).status == 200
        (sent,) = email_module(app).mailer.outbox

    assert sent.message is not None
    assert sent.message.subject == "Hi Ada"
    assert sent.message.html == "<p>Ada</p>"
    assert sent.message.text == "Ada"
    assert sent.recipients == ("ada@example.com",)


async def test_a_file_template_is_read_and_checked_when_the_worker_starts(
    tmp_path: Path,
) -> None:
    template = mail.Template.file(write(tmp_path / "hello.toml"), context=Greeting)
    app = Hinoki(settings=memory_settings(), email_templates=[template])
    assert not template.loaded
    async with ASGIClient(app):
        with mail.use(email_module(app).mailer):
            rendered = template.render(Greeting(name="Ada"))
    assert rendered.subject == "Hi Ada"


async def test_a_file_template_must_be_registered_to_be_sent(tmp_path: Path) -> None:
    template = mail.Template.file(write(tmp_path / "hello.toml"), context=Greeting)
    app = Hinoki(settings=memory_settings())
    async with ASGIClient(app):
        mailer = email_module(app).mailer
        with mail.use(mailer), pytest.raises(RuntimeError, match="not registered"):
            template.message(Greeting(name="Ada"), to=("ada@example.com",))


async def test_an_unknown_variable_in_a_file_stops_the_worker_before_it_serves(
    tmp_path: Path,
) -> None:
    template = mail.Template.file(
        write(tmp_path / "hello.toml", body="<p>{{ name }} {{ nickname }}</p>"),
        context=Greeting,
    )
    app = Hinoki(settings=memory_settings(), email_templates=[template])

    with pytest.raises(RuntimeError, match="failed to start: TemplateError"):
        async with ASGIClient(app):
            pass


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, TemplateReason.UNREADABLE),
        ("x" * (mail.MAX_FILE_BYTES + 1), TemplateReason.TOO_LARGE),
        ("subject = [", TemplateReason.INVALID_FILE),
    ],
)
def test_an_unusable_file_stops_the_mailer_from_opening(
    tmp_path: Path, content: str | None, reason: TemplateReason
) -> None:
    path = tmp_path / "hello.toml"
    if content is not None:
        path.write_text(content)

    with pytest.raises(TemplateError) as caught:
        mail.Mailer(memory_settings().email, [mail.Template.file(path)])

    assert caught.value.reason is reason
    assert str(tmp_path) not in str(caught.value)


def test_an_unknown_variable_in_a_file_names_the_variable(tmp_path: Path) -> None:
    template = mail.Template.file(
        write(tmp_path / "hello.toml", body="<p>{{ name }} {{ nickname }}</p>"),
        context=Greeting,
    )

    with pytest.raises(TemplateError) as caught:
        mail.Mailer(memory_settings().email, [template])

    assert caught.value.reason is TemplateReason.UNKNOWN_VARIABLE
    assert caught.value.variable == "nickname"


def test_registration_refuses_what_cannot_work(tmp_path: Path) -> None:
    other = mail.Template(name="inline", subject="s", html="x")
    with pytest.raises(RegistrationError, match="duplicate"):
        Hinoki(settings=memory_settings(), email_templates=[INLINE, other])
    with pytest.raises(RegistrationError, match="Template objects"):
        Hinoki(settings=memory_settings(), email_templates=["welcome"])  # type: ignore[list-item]
    with pytest.raises(RegistrationError, match="EmailSettings"):
        Hinoki(email_templates=[INLINE])
    with pytest.raises(RegistrationError, match="your own EmailModule"):
        Hinoki(
            settings=memory_settings(),
            modules=[mail.EmailModule(memory_settings().email)],
            email_templates=[INLINE],
        )
    many = [
        mail.Template(name=f"t{i}", subject="s", html="x")
        for i in range(mail.MAX_TEMPLATES + 1)
    ]
    with pytest.raises(RegistrationError, match="at most"):
        Hinoki(settings=memory_settings(), email_templates=many)


async def test_an_own_email_module_takes_its_templates() -> None:
    module = mail.EmailModule(memory_settings().email, templates=[INLINE])
    app = Hinoki(settings=memory_settings(), modules=[module])
    app.include(greet)

    assert module.templates == (INLINE,)
    async with ASGIClient(app) as client:
        assert (await client.post("/greet")).status == 200


async def test_a_refused_render_counts_as_a_failed_send_only_with_a_mailer() -> None:
    untyped = mail.Template(name="notice", subject="{{ day }}", html="<p>x</p>")
    app = Hinoki(settings=memory_settings())
    async with ASGIClient(app):
        mailer = email_module(app).mailer
        token = mail.mailer_var.set(mailer)
        try:
            with pytest.raises(TemplateError):
                untyped.message({}, to=("ada@example.com",))
        finally:
            mail.mailer_var.reset(token)
        assert mailer.stats()["failed"] == 1

    with pytest.raises(TemplateError):
        untyped.message({}, to=("ada@example.com",))
