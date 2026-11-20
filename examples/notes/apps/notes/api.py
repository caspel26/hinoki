"""The Notes endpoints: one route group, public on purpose."""

from typing import Annotated
from uuid import UUID

from hinoki import Field, RouteGroup, Schema, errors

from .models import Note


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]
    body: str = ""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str


notes = RouteGroup("/notes", atomic=True)  # atomic applies to the writes


@notes.get("/")
async def list_notes() -> list[NoteOut]:
    return NoteOut.from_models(await Note.all())


@notes.get("/{note_id}", errors=(errors.NotFound,))
async def get_note(note_id: UUID) -> NoteOut:
    return NoteOut.from_model(await Note.aget(id=note_id))


@notes.post("/", status=201)
async def create_note(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))


@notes.delete("/{note_id}", status=204)
async def delete_note(note_id: UUID) -> None:
    await Note.adelete(id=note_id)
