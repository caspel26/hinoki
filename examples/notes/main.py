"""Declared entry point for the notes example.

Run with ``hinoki dev notes.main:app`` for local SQLite, or set
``NOTES_DATABASE__URL`` to use another backend.
The settings class reads the environment when the command loads this module.
"""

from hinoki import Hinoki

from .app import create, listing, one, remove
from .settings import NotesSettings

app = Hinoki(
    settings=NotesSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    include=[listing, one, create, remove],
)
