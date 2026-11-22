"""A project-wide middleware, listed in ``[project] middleware`` of hinoki.toml."""

from hinoki.http import Middleware, MiddlewareRequest, Next, Response


class ApiVersion(Middleware):
    """Add the API version to every answer of a matched route."""

    def __init__(self, version: str) -> None:
        self.version = version

    async def __call__(self, _request: MiddlewareRequest, call_next: Next) -> Response:
        response = await call_next()
        return response.with_header("x-tracker-version", self.version)


middleware = ApiVersion("1")
