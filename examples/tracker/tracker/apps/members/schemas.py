"""API shapes of the members app."""

from uuid import UUID

from hinoki import Schema


class MemberOut(Schema):
    """Who the token belongs to. The token itself never leaves the server."""

    id: UUID
    name: str
