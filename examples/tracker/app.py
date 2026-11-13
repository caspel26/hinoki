"""Application factory.

    TRACKER_DATABASE_URL=postgres://... \
      granian --interface asgi --factory examples.tracker.app:create_app

Layers (see docs/PATTERNS.md): ``api`` -> ``operations`` -> ``models``.
"""

import os

from examples.tracker import api
from examples.tracker.rules import tracker_rules
from hinoki import HinokiAPI
from hinoki.db import DatabaseModule


def create_app(database_url: str | None = None) -> HinokiAPI:
    app = HinokiAPI(
        title="Tracker",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        rules=tracker_rules,
    )
    app.include(
        api.health, api.list_tasks, api.create_task, api.complete_task, api.delete_task
    )
    app.install(DatabaseModule(database_url or os.environ["TRACKER_DATABASE_URL"]))
    return app
