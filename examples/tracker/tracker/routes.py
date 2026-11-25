"""Project routes: what belongs to no app. Each app's routes come from hinoki.toml."""

from hinoki import Includable, Schema, http


class Health(Schema):
    status: str


@http.get("/health")
async def health() -> Health:
    return Health(status="ok")


routes: list[Includable] = [health]
