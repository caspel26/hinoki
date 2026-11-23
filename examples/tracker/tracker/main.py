"""The Tracker application: everything is read from ``hinoki.toml``.

From ``examples/tracker``: ``uv run hinoki dev``. Local development uses SQLite;
set ``TRACKER_DATABASE__URL`` for another database. Importing this module
reads the settings and opens nothing.
"""

from hinoki import Hinoki

from .commands import Seed
from .settings import TrackerSettings


def create_app(settings: TrackerSettings | None = None) -> Hinoki:
    """Routes, events, middleware and rules come from the manifest."""
    return Hinoki(
        settings=settings or TrackerSettings,
        title="Tracker",
        openapi_url="/openapi.json",
        docs_url="/docs",
        commands=[Seed],
    )


app = create_app()
