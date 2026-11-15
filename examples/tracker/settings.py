"""Tracker settings; local development uses SQLite unless overridden."""

from hinoki import settings


class TrackerSettings(settings.AppSettings, prefix="TRACKER_"):
    database: settings.DatabaseSettings
