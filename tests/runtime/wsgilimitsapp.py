"""A WSGI-hosted application with one sync worker and a queue of one, for the
admission test: a slow sync handler fills both."""

import time

from hinoki import Hinoki, http
from hinoki.settings import AppSettings, ExecutionSettings
from hinoki.wsgi import WSGIAdapter

PAUSE = 1.5


@http.get("/slow")
def slow() -> dict[str, str]:
    time.sleep(PAUSE)
    return {"done": "yes"}


@http.get("/ping")
def ping() -> dict[str, str]:
    return {"ping": "ok"}


def create_app() -> Hinoki:
    app = Hinoki(
        settings=AppSettings(execution=ExecutionSettings(sync_workers=1, sync_queue=1))
    )
    app.include(slow, ping)
    return app


wsgi_app = WSGIAdapter(create_app())
