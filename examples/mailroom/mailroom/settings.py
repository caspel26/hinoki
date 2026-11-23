"""Mailroom settings: only the email group, so no database is needed."""

from hinoki import settings


class MailroomSettings(settings.AppSettings, prefix="MAILROOM_"):
    email: settings.EmailSettings
