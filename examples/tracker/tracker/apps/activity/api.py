"""HTTP layer of the activity app, mounted at ``/activity`` by hinoki.toml."""

from typing import Any

from hinoki import API, Caller, Includable, crud
from hinoki.orm import QuerySet
from tracker.apps.members.security import member_id, member_only

from .models import Activity
from .schemas import ActivityOut


@crud(Activity, path="", read=ActivityOut, access=member_only)
class Feed(API):
    """The caller's activity, newest first. Read only."""

    def get_queryset(self, principal: Caller[Any] | None) -> QuerySet[Activity]:
        return Activity.filter(member_id=member_id(principal))


routes: list[Includable] = [Feed]
