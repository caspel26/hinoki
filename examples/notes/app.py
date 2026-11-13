"""The route and factory form of the smallest useful Hinoki app.

    NOTES_DATABASE__URL=postgres://... \
        uv run hinoki dev examples.notes.main:app

Create the table with a migration (``hinoki migrations plan --models
examples.notes.app``) or by hand; nothing here touches a database at import.
"""

import os
from typing import Annotated
from uuid import UUID, uuid4

from hinoki import Field, Hinoki, Schema, delete, get, post
from hinoki.db import DatabaseModule
from hinoki.orm import Model, field, indexed, ordered_by


@indexed("title")
@ordered_by("title")
class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)
    body: str = ""


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


@get("/notes")
async def listing() -> list[NoteOut]:
    return [NoteOut.from_model(n) for n in await Note.all().aall()]


@get("/notes/{note_id}")
async def one(note_id: UUID) -> NoteOut:
    return NoteOut.from_model(await Note.aget(id=note_id))


@post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))


@delete("/notes/{note_id}", status=204, transaction=True)
async def remove(note_id: UUID) -> None:
    await Note.adelete(id=note_id)


def create_app(database_url: str | None = None) -> Hinoki:
    """Factory kept for tests that pass a disposable database URL explicitly."""
    app = Hinoki(title="Notes", openapi_url="/openapi.json", docs_url="/docs")
    app.include(listing, one, create, remove)
    app.install(DatabaseModule(database_url or os.environ["NOTES_DATABASE_URL"]))
    return app
