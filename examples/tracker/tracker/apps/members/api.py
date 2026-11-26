"""HTTP layer of the members app, mounted at ``/members`` in ``tracker/routes.py``."""

from hinoki import Caller, Includable, http

from .models import Member
from .schemas import MemberOut
from .security import member_only


@http.get("/me", access=member_only)
async def me(caller: Caller[Member]) -> MemberOut:
    """The member the bearer token belongs to."""
    return MemberOut.from_model(caller.user)


routes: list[Includable] = [me]
