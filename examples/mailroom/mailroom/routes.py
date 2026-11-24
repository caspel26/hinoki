"""The routes: each renders a template into a message and sends it.

Sending mail on request is an abuse target, so every route requires an API key
(``x-api-key``, the ``MAILROOM_API_KEY`` setting) and the request fields carry the
same bounds as the message contexts, which the OpenAPI document then shows.
"""

import hmac
from typing import Annotated

from hinoki import Access, Caller, Field, Includable, Schema, errors, http, mail
from hinoki.auth import APIKeyAuth
from hinoki.crypto import random_token
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
from .settings import MailroomSettings

START = "https://example.com/start"

Name = Annotated[str, Field(min_length=1, max_length=80)]
Address = Annotated[Email, Field(max_length=254)]


class SignUp(Schema):
    email: Address
    name: Name
    trial_days: Annotated[int, Field(ge=0, le=365)] = 14


class PasswordReset(Schema):
    email: Address
    name: Name
    requested_from: Annotated[str | None, Field(max_length=80)] = None
    devices: Annotated[list[Name], Field(max_length=10)]


class Notice(Schema):
    email: Address
    values: Annotated[
        dict[str, Annotated[str, Field(max_length=200)]], Field(max_length=20)
    ]


class Sent(Schema):
    message_id: str


class MailKey(APIKeyAuth[None]):
    """The caller of Mailroom is a trusted service holding the configured key."""

    def __init__(self, key: str) -> None:
        self.key = key.encode()

    def resolve(self, value: str) -> Caller[None] | None:
        if hmac.compare_digest(value.encode(), self.key):
            return Caller(id="mail-client", user=None)
        return None


def build_routes(config: MailroomSettings) -> list[Includable]:
    """The routes, closed over the API key and the base URL of reset links."""
    access = Access(auth=MailKey(config.api_key.reveal()))
    base_url = config.public_base_url.rstrip("/")

    @http.post("/signup", status=202, access=access)
    async def sign_up(data: SignUp) -> Sent:
        """Send the welcome message of a new account."""
        welcome = Welcome(
            user=Account(name=data.name, trial_days=data.trial_days),
            steps=[Step(title="Create a project", url=START)],
        )
        sent = await mail.send(WELCOME.message(welcome, to=(data.email,)))
        return Sent(message_id=sent.message_id)

    @http.post("/password-reset", status=202, access=access)
    async def reset_password(data: PasswordReset) -> Sent:
        """Send a password reset link built here, never taken from the request.

        A real application stores the token (hashed, single use, with an expiry)
        before sending it; this example has no database and keeps nothing.
        """
        context = ResetPassword(
            name=data.name,
            link=f"{base_url}/reset?token={random_token()}",
            requested_from=data.requested_from,
            devices=[Device(name=name) for name in data.devices],
        )
        sent = await mail.send(RESET_PASSWORD.message(context, to=(data.email,)))
        return Sent(message_id=sent.message_id)

    @http.post(
        "/notices/{slug}",
        status=202,
        access=access,
        errors=(errors.NotFound, errors.BadRequest),
    )
    async def send_notice(slug: str, data: Notice) -> Sent:
        """Send a notice stored as strings; its template takes a plain dict."""
        if slug not in NOTICES:
            raise errors.NotFound("There is no such notice.")
        try:
            message = notice_template(slug).message(data.values, to=(data.email,))
        except mail.TemplateError:
            raise errors.BadRequest(
                "The notice cannot be rendered with these values.",
                code="invalid_notice",
            ) from None
        return Sent(message_id=(await mail.send(message)).message_id)

    return [sign_up, reset_password, send_notice]
