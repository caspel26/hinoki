"""Persistence layer: the member table. Knows nothing about HTTP."""

from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, field, pk


@db.table("tracker_members")
class Member(Model):
    id: UUID = pk(default_factory=uuid4)
    name: str = field(max_length=80)
    token_hash: str = field(unique=True)  # SHA-256 hex digest, never the token
