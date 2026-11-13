"""Declared entry point for the notes example.

Run with ``NOTES_DATABASE__URL=postgres://... hinoki dev examples.notes.main:app``.
The settings class reads the environment when the command loads this module.
"""

from examples.notes.app import create, listing, one, remove
from hinoki import DatabaseSettings, FrameworkSettings, Hinoki
from hinoki.db import DatabaseModule


class NotesSettings(FrameworkSettings, prefix="NOTES_"):
    database: DatabaseSettings


app = Hinoki(
    settings=NotesSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    include=[listing, one, create, remove],
    modules=[DatabaseModule],
)
