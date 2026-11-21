"""HTTP action decorators: ``@http.get("/tasks")`` and the other verbs.

    from hinoki import http

    @http.get("/tasks/{task_id}", access=member_only, errors=(NotFound,))
    async def one(task_id: UUID) -> TaskOut: ...

The implementation lives in ``hinoki.routing`` with the route model; this
package is the public surface for the verbs and HTTP request/response types.
``query`` is the HTTP QUERY method: a safe request that carries a body.
"""

from hinoki.http.middleware import Middleware, MiddlewareRequest, Next
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.http.stream import Event, EventStream, File, Stream
from hinoki.routing import delete, get, patch, post, put, query

__all__ = [
    "Event",
    "EventStream",
    "File",
    "Middleware",
    "MiddlewareRequest",
    "Next",
    "Request",
    "Response",
    "Stream",
    "delete",
    "get",
    "patch",
    "post",
    "put",
    "query",
]
