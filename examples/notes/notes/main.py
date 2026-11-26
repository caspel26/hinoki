"""The Notes application: settings and the routes, nothing else.

Importing this module reads the settings and opens nothing.
"""

from hinoki import Hinoki

from .settings import NotesSettings


def create_app(settings: NotesSettings | type[NotesSettings] = NotesSettings) -> Hinoki:
    """A fresh application; tests pass settings that point at their own database."""
    return Hinoki(
        settings=settings,
        title="Notes",
        openapi_url="/openapi.json",
        docs_url="/docs",
    )


app = create_app()
