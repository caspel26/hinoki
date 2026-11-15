"""The route and factory form of the smallest useful Hinoki app.

    uv run --project .. hinoki dev notes.main:app

The examples project manifest registers this model and its migration package;
nothing here touches a database at import.
"""

from typing import Annotated
from uuid import UUID

from hinoki import Field, Hinoki, Schema, http
from hinoki.db import DatabaseModule

from .models import Note
from .settings import NotesSettings


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


@http.get("/notes")
async def listing() -> list[NoteOut]:
    return NoteOut.from_models(await Note.all())


@http.get("/notes/{note_id}")
async def one(note_id: UUID) -> NoteOut:
    return NoteOut.from_model(await Note.aget(id=note_id))


@http.post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))


@http.delete("/notes/{note_id}", status=204, transaction=True)
async def remove(note_id: UUID) -> None:
    await Note.adelete(id=note_id)


def create_app(database_url: str | None = None) -> Hinoki:
    """Factory kept for tests that pass a disposable database URL explicitly."""
    overrides = {"database": {"url": database_url}} if database_url is not None else {}
    app_settings = NotesSettings.load(overrides=overrides)
    app = Hinoki(
        settings=app_settings,
        title="Notes",
        openapi_url="/openapi.json",
        docs_url="/docs",
    )
    app.include(listing, one, create, remove)
    app.install(DatabaseModule(app_settings.database.url.reveal()))
    return app
