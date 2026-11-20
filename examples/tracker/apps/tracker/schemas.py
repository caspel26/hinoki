"""API shapes. Separate from the models: a column reaches a client only here."""

from typing import Annotated

from hinoki import Field, Schema, derive

from .models import Task


class TaskIn(Schema):
    """What a client may set. The owner is the caller, never a field."""

    title: Annotated[str, Field(min_length=1, max_length=120)]


@derive(Task, exclude=("owner_id",))
class TaskOut(Schema):
    """Every column of a task except its owner (derived from the model)."""
