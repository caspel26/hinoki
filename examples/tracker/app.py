"""Application factory.

The MCP endpoint is derived from the assembled API, so this example composes
the app in a factory before exposing the allowlisted action.

    TRACKER_DATABASE_URL=postgres://... \
      hinoki dev examples.tracker.app:create_app

Layers (see docs/PATTERNS.md): ``api`` -> ``operations`` -> ``models``.
"""

import os

from examples.tracker import api
from examples.tracker.rules import tracker_rules
from examples.tracker.security import member_only
from hinoki import Hinoki
from hinoki.db import DatabaseModule
from hinoki.mcpapp import expose_as_mcp


def create_app(database_url: str | None = None) -> Hinoki:
    app = Hinoki(
        title="Tracker",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        rules=tracker_rules,
    )
    app.include(api.health, api.tasks)
    # The MCP endpoint exposes only this action and uses the caller's own token.
    app.include(
        *expose_as_mcp(app, tools=[f"{api.__name__}.list_tasks"], access=member_only)
    )
    app.install(DatabaseModule(database_url or os.environ["TRACKER_DATABASE_URL"]))
    return app
