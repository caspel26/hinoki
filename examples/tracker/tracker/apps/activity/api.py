"""HTTP layer of the activity app, mounted at ``/activity`` by hinoki.toml."""

from hinoki import API, Caller, Includable, crud
from hinoki.orm import QuerySet
from tracker.apps.members.models import Member
from tracker.apps.members.security import member_only

from .models import Activity
from .schemas import ActivityOut


@crud(Activity, path="", read=ActivityOut, access=member_only)
class Feed(API):
    """The caller's activity, newest first. Read only."""

    def get_queryset(self, caller: Caller[Member]) -> QuerySet[Activity]:
        return Activity.filter(member_id=caller.user.id)


routes: list[Includable] = [Feed]
