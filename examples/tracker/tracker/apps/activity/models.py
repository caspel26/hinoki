"""Persistence layer of the activity app."""

from datetime import datetime
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.utils import utc_now
from tracker.apps.members.models import Member


@db.indexed("member_id", "created_at")
@db.ordered_by("-created_at", "-id")
@db.table("tracker_activity")
class Activity(Model, label="Activity", plural="Activity"):
    id: UUID = pk(default_factory=uuid4)
    member_id: UUID = foreign_key(
        Member, on_delete=OnDelete.CASCADE, related_name="activity"
    )
    task_id: UUID
    kind: str = field(max_length=20)
    created_at: datetime = field(default_factory=utc_now)
