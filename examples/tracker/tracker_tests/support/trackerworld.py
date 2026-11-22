"""What every tracker test starts from: a running app and two members."""

from dataclasses import dataclass

from tracker.apps.members.models import Member

from hinoki.testing import ASGIClient


@dataclass(frozen=True)
class World:
    client: ASGIClient
    ann: Member
    ben: Member
