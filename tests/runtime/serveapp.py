"""An application whose port comes from its own settings (for the serve test)."""

from hinoki import Hinoki, http
from hinoki.settings import AppSettings


class ServeSettings(AppSettings, prefix="SRV_"):
    pass


@http.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


def create_app() -> Hinoki:
    app = Hinoki(settings=ServeSettings.load())  # prefix declared on the class
    app.include(ping)
    return app
