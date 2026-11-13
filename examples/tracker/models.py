"""Persistence layer: tables and relations. Knows nothing about HTTP."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from hinoki.orm import Model, field, foreign_key


def now() -> datetime:
    return datetime.now(UTC)


class Member(Model, table="tracker_members"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    name: str = field(max_length=80)
    token: str = field(unique=True)


class Task(Model, table="tracker_tasks"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="tasks")
    title: str = field(max_length=120)
    done: bool = False
    created_at: datetime = field(default_factory=now)
    completed_at: datetime | None = None
