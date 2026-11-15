"""Application factory.

The MCP endpoint is derived from the assembled API, so this example composes
the app in a factory before exposing the allowlisted action.

    hinoki dev examples.tracker.app:create_app

Set ``TRACKER_DATABASE__URL`` to use a server database instead of SQLite.

Layers (see docs/patterns.md): ``api`` -> ``operations`` -> ``models``.
"""

from examples.tracker import api
from examples.tracker.rules import tracker_rules
from examples.tracker.security import member_only
from examples.tracker.settings import TrackerSettings
from hinoki import Hinoki
from hinoki.db import DatabaseModule
from hinoki.mcpapp import expose_as_mcp


def create_app(database_url: str | None = None) -> Hinoki:
    overrides = {"database": {"url": database_url}} if database_url is not None else {}
    app_settings = TrackerSettings.load(overrides=overrides)
    app = Hinoki(
        settings=app_settings,
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
    app.install(
        DatabaseModule(
            app_settings.database.url.reveal(),
            max_size=app_settings.database.max_size,
        )
    )
    return app
