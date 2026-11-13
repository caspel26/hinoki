"""HTTP layer: shapes, status codes, access and transactions. No business rules."""

from uuid import UUID

from examples.tracker import operations
from examples.tracker.errors import DoneTasksAreKept, TaskAlreadyDone
from examples.tracker.models import Member, Task
from examples.tracker.schemas import TaskIn, TaskOut
from examples.tracker.security import member_only
from hinoki import Caller, NotFound, RouteGroup, get
from hinoki.pagination import Page, PageNumberPagination, PageParams


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


@get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


tasks = RouteGroup(
    "/tasks", access=member_only, atomic=True
)  # stated once for the group


@tasks.get("/", pagination=PageNumberPagination(page_size=10, max_page_size=50))
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(out)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return out(await operations.create_task(caller.user.id, data))


@tasks.post("/{task_id}/complete", errors=(NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))


@tasks.delete("/{task_id}", status=204, errors=(NotFound, DoneTasksAreKept))
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)
