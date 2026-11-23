"""The routes: each renders a template into a message and sends it."""

from hinoki import Includable, Schema, errors, http, mail
from hinoki.fields import Email

from .messages import (
    RESET_PASSWORD,
    WELCOME,
    Account,
    Device,
    ResetPassword,
    Step,
    Welcome,
)
from .notices import NOTICES, notice_template

START = "https://example.com/start"


class SignUp(Schema):
    email: Email
    name: str
    trial_days: int = 14


class PasswordReset(Schema):
    email: Email
    name: str
    link: str
    requested_from: str | None = None
    devices: list[str]


class Notice(Schema):
    email: Email
    values: dict[str, str]


class Sent(Schema):
    message_id: str


@http.post("/signup", status=202)
async def sign_up(data: SignUp) -> Sent:
    """Send the welcome message of a new account."""
    welcome = Welcome(
        user=Account(name=data.name, trial_days=data.trial_days),
        steps=[Step(title="Create a project", url=START)],
    )
    sent = await mail.send(WELCOME.message(welcome, to=(data.email,)))
    return Sent(message_id=sent.message_id)


@http.post("/password-reset", status=202)
async def reset_password(data: PasswordReset) -> Sent:
    """Send a password reset link."""
    context = ResetPassword(
        name=data.name,
        link=data.link,
        requested_from=data.requested_from,
        devices=[Device(name=name) for name in data.devices],
    )
    sent = await mail.send(RESET_PASSWORD.message(context, to=(data.email,)))
    return Sent(message_id=sent.message_id)


@http.post("/notices/{slug}", status=202, errors=(errors.NotFound, errors.BadRequest))
async def send_notice(slug: str, data: Notice) -> Sent:
    """Send a notice stored as strings; its template takes a plain dict."""
    if slug not in NOTICES:
        raise errors.NotFound("There is no such notice.")
    try:
        message = notice_template(slug).message(data.values, to=(data.email,))
    except mail.TemplateError:
        raise errors.BadRequest(
            "The notice cannot be rendered with these values.", code="invalid_notice"
        ) from None
    return Sent(message_id=(await mail.send(message)).message_id)


routes: list[Includable] = [sign_up, reset_password, send_notice]
