"""The Tracker application.

From ``examples/``: ``uv run hinoki dev tracker.main:app``. Local development
uses SQLite; set ``TRACKER_DATABASE__URL`` for another database. Importing this
module reads the settings and opens nothing.

Layers (see docs/patterns.md): ``api`` -> ``operations`` -> ``models``.
"""

from hinoki import Hinoki
from hinoki.ai.mcpapp import expose_as_mcp

from . import api
from .rules import tracker_rules
from .security import member_only
from .settings import TrackerSettings

app = Hinoki(
    settings=TrackerSettings,
    title="Tracker",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    include=[api.health, api.tasks],
    rules=tracker_rules,
)
# The MCP endpoint exposes only this action and uses the caller's own token.
app.include(
    *expose_as_mcp(app, tools=[f"{api.__name__}.list_tasks"], access=member_only)
)
