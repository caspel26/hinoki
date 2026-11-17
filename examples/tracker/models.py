"""Persistence layer: tables and relations. Knows nothing about HTTP."""

from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now


@db.table("tracker_members")
class Member(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)


@db.indexed("owner_id", "created_at")
@db.ordered_by("-created_at", "-id")
@db.table("tracker_tasks")
class Task(Model, label="Task", plural="Tasks"):
    id: UUID = pk(default_factory=uuid4)
    owner_id: UUID = foreign_key(
        Member, on_delete=OnDelete.RESTRICT, related_name="tasks"
    )
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
