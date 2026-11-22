"""What a client sends and receives; a column reaches a client only here."""

from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema, derive

from .models import Note


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80, strip=True)]
    body: Annotated[str, Field(max_length=10_000)] = ""


@derive(Note, only=("title", "body"), partial=True)
class NotePatch(Schema):
    """Any subset of the writable fields, derived from the model."""


class NoteOut(Schema):
    id: UUID
    title: str
    body: str
