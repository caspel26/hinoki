"""HTTP layer of the tasks app: generated CRUD plus one handwritten action."""

from uuid import UUID

from hinoki import API, Caller, Includable, crud, errors, http
from hinoki.orm import QuerySet
from tracker.apps.members.models import Member
from tracker.apps.members.security import member_only

from . import operations
from .errors import TaskAlreadyDone
from .filters import TaskFilters
from .models import Task
from .schemas import TaskIn, TaskOut


@crud(
    Task,
    path="",
    read=TaskOut,
    create=TaskIn,
    delete=True,
    access=member_only,
    owner="owner_id",
    filters=TaskFilters,
)
class Tasks(API):
    """The caller's own tasks: list (filtered), retrieve, create and delete."""

    def get_queryset(self, caller: Caller[Member]) -> QuerySet[Task]:
        return operations.mine(caller.user.id)

    async def remove(
        self, item: Task, _scoped: QuerySet[Task], caller: Caller[Member]
    ) -> bool:
        await operations.delete_task(caller.user.id, item.id)
        return True

    @http.post(
        "/{item_id}/complete",
        access=member_only,
        atomic=True,
        errors=(errors.NotFound, TaskAlreadyDone),
    )
    async def complete(self, item_id: UUID, caller: Caller[Member]) -> TaskOut:
        """Mark an own open task done."""
        return TaskOut.from_model(
            await operations.complete_task(caller.user.id, item_id)
        )


routes: list[Includable] = [Tasks]
