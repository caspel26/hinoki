"""Notes settings; local development uses SQLite unless overridden."""

from hinoki import settings


class NotesSettings(settings.AppSettings, prefix="NOTES_"):
    database: settings.DatabaseSettings
