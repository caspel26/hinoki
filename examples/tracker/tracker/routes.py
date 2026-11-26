"""Project route map: explicit mounts for every exposed app."""

from hinoki import Includable, Mount, Schema, http

from .apps.activity.api import routes as activity_routes
from .apps.members.api import routes as members_routes
from .apps.tasks.api import routes as tasks_routes


class Health(Schema):
    status: str


@http.get("/health")
async def health() -> Health:
    return Health(status="ok")


routes: list[Includable] = [
    health,
    Mount("/members", members_routes),
    Mount("/tasks", tasks_routes),
    Mount("/activity", activity_routes),
]
