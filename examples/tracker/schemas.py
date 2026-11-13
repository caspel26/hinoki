"""API shapes. Separate from the models: a column reaches a client only here."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from hinoki import Field, Schema


class TaskIn(Schema):
    """What a client may set. The owner is the caller, never a field."""

    title: Annotated[str, Field(min_length=1, max_length=120)]


class TaskOut(Schema):
    id: UUID
    title: str
    done: bool
    created_at: datetime
    completed_at: datetime | None
