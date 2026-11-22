"""An application with metrics on, for running the Rust server in a subprocess:
the metrics port comes from ``METRICS_PORT``."""

import os

from hinoki import Hinoki, http
from hinoki.settings import AppSettings, MetricsSettings


@http.get("/items/{item_id}")
async def item(item_id: int) -> dict[str, int]:
    return {"id": item_id}


def create_app() -> Hinoki:
    metrics = MetricsSettings(listen=f"127.0.0.1:{os.environ['METRICS_PORT']}")
    app = Hinoki(settings=AppSettings(metrics=metrics))
    app.include(item)
    return app
