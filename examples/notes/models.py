"""Notes-owned table declarations."""

from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, pk


@db.ordered_by("title")
@db.table("notes")
class Note(Model):
    id: UUID = pk(default_factory=uuid4)
    title: str = field(max_length=80, index=True)
    body: str = ""
