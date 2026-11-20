"""The Notes application.

From ``examples/``: ``uv run hinoki dev notes.main:app``. Local development uses
SQLite; set ``NOTES_DATABASE__URL`` for another database. Importing this module
reads the settings and opens nothing.
"""

from hinoki import Hinoki

from .api import notes
from .settings import NotesSettings

app = Hinoki(
    settings=NotesSettings,
    title="Notes",
    openapi_url="/openapi.json",
    docs_url="/docs",
    include=[notes],
)
