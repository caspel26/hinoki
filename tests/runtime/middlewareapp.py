"""The application the middleware parity test serves under each server."""

from hinoki import Hinoki, http
from hinoki.errors import NotFound
from hinoki.http import Middleware, MiddlewareRequest, Next, Response


class Tag(Middleware):
    def __init__(self, label: str) -> None:
        self.label = label

    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        return (await call_next()).with_header(f"x-{self.label}", request.route)


class Guard(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        if "x-allow" not in request.headers:
            return Response(403, "no", content_type="text/plain")
        return await call_next()


class Raises(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        raise NotFound()


class Head(Middleware):
    async def __call__(self, request: MiddlewareRequest, call_next: Next) -> Response:
        seen = (request.method, request.path, request.route, request.query)
        return Response(200, "|".join(seen), content_type="text/plain")


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {"n": 1}


@http.get("/tagged", middleware=[Tag("route")])
async def tagged() -> dict[str, int]:
    return {"n": 1}


@http.get("/guarded", middleware=[Guard()])
async def guarded() -> dict[str, int]:
    return {"n": 1}


@http.get("/missing", middleware=[Tag("route")])
async def missing() -> dict[str, int]:
    raise NotFound()


@http.get("/boom", middleware=[Tag("route")])
async def boom() -> dict[str, int]:
    raise RuntimeError("secret detail")


@http.get("/raises", middleware=[Raises()])
async def raises() -> dict[str, int]:
    return {"n": 1}


@http.get("/head/{item_id}", middleware=[Head()])
async def head(item_id: int) -> dict[str, int]:
    return {"n": item_id}


def create_app() -> Hinoki:
    return Hinoki(
        include=[plain, tagged, guarded, missing, boom, raises, head],
        middleware=[Tag("app")],
        openapi_url="/openapi.json",
    )
