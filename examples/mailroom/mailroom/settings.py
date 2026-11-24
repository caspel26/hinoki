"""Mailroom settings: the email group and the sender's credentials, no database."""

from hinoki import settings


class MailroomSettings(settings.AppSettings, prefix="MAILROOM_"):
    email: settings.EmailSettings
    api_key: settings.Secret  # the key callers send in ``x-api-key``
    public_base_url: str = "https://example.com"  # where reset links point
