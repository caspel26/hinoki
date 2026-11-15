"""Notes-owned table declarations."""

from uuid import UUID, uuid4

from hinoki.orm import Model, field, indexed, ordered_by


@indexed("title")
@ordered_by("title")
class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)
    body: str = ""
