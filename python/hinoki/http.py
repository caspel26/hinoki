"""HTTP action decorators: ``@http.get("/tasks")`` and the other verbs.

    from hinoki import http

    @http.get("/tasks/{task_id}", access=member_only, errors=(NotFound,))
    async def one(task_id: UUID) -> TaskOut: ...

The implementation lives in ``hinoki.routing`` with the route model; this module
is the public surface for the five verbs.
"""

from hinoki.routing import delete, get, patch, post, put

__all__ = ["delete", "get", "patch", "post", "put"]
