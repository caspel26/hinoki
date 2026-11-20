"""Authentication: an opaque bearer token resolved to a ``Member``."""

from hinoki import Access, BearerAuth, Caller, Permission

from .models import Member


class TrackerPermission(Permission):
    USE = "tasks:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token=token)
        if member is None:
            return None
        return Caller(
            id=str(member.id), user=member, permissions={TrackerPermission.USE}
        )


member_only = Access(auth=MemberTokens(), permissions=[TrackerPermission.USE])
