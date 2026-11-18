"""Tables of the admin's users. Their names are fixed, so a class rename never
moves them; the checked-in migrations of this package create them."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from hinoki import db
from hinoki.orm import Model, OnDelete, field, foreign_key, pk
from hinoki.orm.relations import Related
from hinoki.utils import utc_now


class AdminUser(Model, table="hinoki_admin_users"):
    id: UUID = pk(default_factory=uuid4)
    username: str = field(unique=True, max_length=150)
    password_hash: str = field(max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    created_at: datetime = field(default_factory=utc_now)
    last_login: datetime | None = None
    sessions: ClassVar["Related[list[AdminSession]]"]
    grants: ClassVar["Related[list[AdminGrant]]"]


@db.indexed("user_id")
class AdminSession(Model, table="hinoki_admin_sessions"):
    id: UUID = pk(default_factory=uuid4)
    token_hash: str = field(unique=True, max_length=64)  # SHA-256 of the cookie value
    user_id: UUID = foreign_key(
        AdminUser, on_delete=OnDelete.CASCADE, related_name="sessions"
    )
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=utc_now)
    last_seen_at: datetime = field(default_factory=utc_now)
    user_agent: str = field(default="", max_length=200)
    # SHA-256 of the user's password hash when the session opened: a changed
    # password, even one made while a sign-in was in flight, ends the session.
    fingerprint: str = field(default="", max_length=64)


@db.indexed("user_id")
class AdminGrant(
    Model, table="hinoki_admin_grants", unique=[("user_id", "model", "action")]
):
    id: UUID = pk(default_factory=uuid4)
    user_id: UUID = foreign_key(
        AdminUser, on_delete=OnDelete.CASCADE, related_name="grants"
    )
    model: str = field(max_length=80)  # the admin slug of the model
    action: str = field(max_length=20)  # view, add, change or delete
