"""HTTP layer: shapes, status codes, access and transactions. No business rules."""

from uuid import UUID

from hinoki import Caller, RouteGroup, errors, http
from hinoki.pagination import Page, PageNumberPagination, PageParams

from . import operations
from .errors import DoneTasksAreKept, TaskAlreadyDone
from .models import Member
from .schemas import TaskIn, TaskOut
from .security import member_only


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


tasks = RouteGroup("/tasks", access=member_only, atomic=True)


@tasks.get("/", pagination=PageNumberPagination(page_size=10, max_page_size=50))
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(TaskOut.from_model)


@tasks.post("/", status=201)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return TaskOut.from_model(await operations.create_task(caller.user.id, data))


@tasks.post("/{task_id}/complete", errors=(errors.NotFound, TaskAlreadyDone))
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return TaskOut.from_model(await operations.complete_task(caller.user.id, task_id))


@tasks.delete("/{task_id}", status=204, errors=(errors.NotFound, DoneTasksAreKept))
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)
