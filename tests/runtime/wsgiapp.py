"""An application served through the WSGI adapter."""

from hinoki import Hinoki, Request, Schema, http
from hinoki.wsgi import WSGIAdapter

LOG: list[str] = []


class Echo(Schema):
    text: str


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "async"}


@http.get("/sync")
def sync_ping() -> dict[str, str]:
    return {"pong": "sync"}


@http.post("/echo", status=201)
async def echo(request: Request, data: Echo) -> Echo:
    return data


def create_app() -> Hinoki:
    app = Hinoki()
    app.include(ping, sync_ping, echo)
    return app


wsgi_app = WSGIAdapter(create_app())
