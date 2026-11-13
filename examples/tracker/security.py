"""Authentication: an opaque bearer token resolved to a ``Member``."""

from examples.tracker.models import Member
from hinoki import Access, BearerAuth, Caller

USE = "tasks:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token=token)
        if member is None:
            return None
        return Caller(id=str(member.id), user=member, permissions=frozenset({USE}))


member_only = Access(auth=MemberTokens(), permissions=[USE])
