"""HTTP layer: shapes, status codes, access and transactions. No business rules."""

from uuid import UUID

from hinoki import Caller, RouteGroup, errors, http
from hinoki.pagination import Page, PageNumberPagination, PageParams

from . import operations
from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Member, Task
from .schemas import TaskIn, TaskOut
from .security import member_only


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


tasks = RouteGroup(
    "/tasks", access=member_only, atomic=True
)  # stated once for the group


@tasks.get(
    "/",
    pagination=PageNumberPagination(page_size=10, max_page_size=50),
    validate_response=True,  # a result that is not a Page of TaskOut is a 500
)
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(out)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return await operations.create_task(caller.user.id, data)  # type: ignore[return-value]


@tasks.post(
    "/{task_id}/complete",
    errors=(errors.NotFound, TaskAlreadyDone),
    validate_response=True,  # the Task is projected, then must be a TaskOut
)
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return await operations.complete_task(caller.user.id, task_id)  # type: ignore[return-value]


@tasks.delete("/{task_id}", status=204, errors=(errors.NotFound, DoneTasksAreKept))
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)
