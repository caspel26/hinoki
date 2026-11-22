"""API shapes of the activity app."""

from datetime import datetime
from uuid import UUID

from hinoki import Schema


class ActivityOut(Schema):
    id: UUID
    task_id: UUID
    kind: str
    created_at: datetime
