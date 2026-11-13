"""An application whose port comes from its own settings (for the serve test)."""

from hinoki import FrameworkSettings, Hinoki, get


class ServeSettings(FrameworkSettings, prefix="SRV_"):
    pass


@get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


def create_app() -> Hinoki:
    app = Hinoki(settings=ServeSettings.load())  # prefix declared on the class
    app.include(ping)
    return app
