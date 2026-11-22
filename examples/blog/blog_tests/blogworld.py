"""What every blog test starts from: a running app and synthetic secrets."""

from dataclasses import dataclass
from uuid import UUID

from hinoki.testing import ASGIClient

ADMIN_SECRET = "a" * 40
JWT_SECRET = "j" * 40
PASSWORD = "correct horse battery staple"


@dataclass(frozen=True)
class World:
    client: ASGIClient
    ada_id: UUID
    url: str
