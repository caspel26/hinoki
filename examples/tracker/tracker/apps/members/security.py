"""Authentication: an opaque bearer token resolved to a ``Member``."""

import hashlib

from hinoki import Access, BearerAuth, Caller, Permission

from .models import Member


def hash_token(token: str) -> str:
    """The SHA-256 hex digest stored instead of the token (high-entropy, so no KDF)."""
    return hashlib.sha256(token.encode()).hexdigest()


class MemberPermission(Permission):
    USE = "tracker:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token_hash=hash_token(token))
        if member is None:
            return None
        return Caller(
            id=str(member.id), user=member, permissions={MemberPermission.USE}
        )


member_only = Access(auth=MemberTokens(), permissions=[MemberPermission.USE])
