"""Declared entry point for the notes example.

Run with ``hinoki dev examples.notes.main:app`` for local SQLite, or set
``NOTES_DATABASE__URL`` to use another backend.
The settings class reads the environment when the command loads this module.
"""

from examples.notes.app import create, listing, one, remove
from examples.notes.settings import NotesSettings
from hinoki import Hinoki
from hinoki.db import DatabaseModule

app = Hinoki(
    settings=NotesSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    include=[listing, one, create, remove],
    modules=[DatabaseModule],
)
