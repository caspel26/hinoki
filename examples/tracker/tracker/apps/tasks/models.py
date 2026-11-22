"""Persistence layer of the tasks app. Knows nothing about HTTP."""

from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now
from tracker.apps.members.models import Member


@db.indexed("owner_id", "created_at")
@db.ordered_by("-created_at", "-id")
@db.table("tracker_tasks")
class Task(Model, label="Task", plural="Tasks"):
    id: UUID = pk(default_factory=uuid4)
    owner_id: UUID = foreign_key(
        Member, on_delete=OnDelete.RESTRICT, related_name="tasks"
    )
    title: str = field(max_length=120)
    priority: int = 3
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
