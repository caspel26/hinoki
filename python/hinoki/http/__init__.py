"""HTTP action decorators: ``@http.get("/tasks")`` and the other verbs.

    from hinoki import http

    @http.get("/tasks/{task_id}", access=member_only, errors=(NotFound,))
    async def one(task_id: UUID) -> TaskOut: ...

The implementation lives in ``hinoki.routing`` with the route model; this
package is the public surface for the verbs and HTTP request/response types.
``query`` is the HTTP QUERY method: a safe request that carries a body.
``websocket`` declares an action served over an upgraded connection (see
``hinoki.websocket``).
"""

from hinoki.http.cors import Cors
from hinoki.http.middleware import Middleware, MiddlewareRequest, Next
from hinoki.http.request import Request
from hinoki.http.response import Response
from hinoki.http.stream import Event, EventStream, File, Stream
from hinoki.routing import delete, get, patch, post, put, query
from hinoki.websocket import CloseCode, WebSocket, WebSocketClosed, websocket

__all__ = [
    "CloseCode",
    "Cors",
    "Event",
    "EventStream",
    "File",
    "Middleware",
    "MiddlewareRequest",
    "Next",
    "Request",
    "Response",
    "Stream",
    "WebSocket",
    "WebSocketClosed",
    "delete",
    "get",
    "patch",
    "post",
    "put",
    "query",
    "websocket",
]
