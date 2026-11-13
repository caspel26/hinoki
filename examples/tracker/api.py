"""HTTP layer: shapes, status codes, access and transactions. No business rules."""

from uuid import UUID

from examples.tracker import operations
from examples.tracker.models import Member, Task
from examples.tracker.schemas import TaskIn, TaskOut
from examples.tracker.security import member_only
from hinoki import Caller, delete, get, post
from hinoki.pagination import Page, PageNumberPagination, PageParams


def out(task: Task) -> TaskOut:
    return TaskOut.from_model(task)


@get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@get(
    "/tasks",
    access=member_only,
    pagination=PageNumberPagination(page_size=10, max_page_size=50),
)
async def list_tasks(page: PageParams, caller: Caller[Member]) -> Page[TaskOut]:
    """Your tasks, newest first."""
    return (await operations.list_tasks(caller.user.id, page)).map(out)


@post("/tasks", status=201, access=member_only, transaction=True)
async def create_task(data: TaskIn, caller: Caller[Member]) -> TaskOut:
    return out(await operations.create_task(caller.user.id, data))


@post("/tasks/{task_id}/complete", access=member_only, transaction=True)
async def complete_task(task_id: UUID, caller: Caller[Member]) -> TaskOut:
    return out(await operations.complete_task(caller.user.id, task_id))


@delete("/tasks/{task_id}", status=204, access=member_only, transaction=True)
async def delete_task(task_id: UUID, caller: Caller[Member]) -> None:
    await operations.delete_task(caller.user.id, task_id)
