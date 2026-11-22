"""Authentication: an opaque bearer token resolved to a ``Member``."""

from typing import Any, cast
from uuid import UUID

from hinoki import Access, BearerAuth, Caller, Permission

from .models import Member


def member_id(principal: Caller[Any] | None) -> UUID:
    """The caller's member id, for a hook of an API whose actions are member_only."""
    return cast("Caller[Member]", principal).user.id


class MemberPermission(Permission):
    USE = "tracker:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token=token)
        if member is None:
            return None
        return Caller(
            id=str(member.id), user=member, permissions={MemberPermission.USE}
        )


member_only = Access(auth=MemberTokens(), permissions=[MemberPermission.USE])
