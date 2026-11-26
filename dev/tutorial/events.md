# 5. Events between apps

Creating a task and completing one both leave a line in the member's activity
feed, but `tasks` does not know the feed exists. It announces: generated create
emits `Created[Task]`, and `complete_task` ([chapter 4](https://hinoki.caspel.dev/dev/tutorial/3-operations.md)) emits
`Updated[Task]` itself because `aupdate_where` emits no model event. The
`activity` app listens:

```python
# tracker/apps/activity/events.py
from hinoki import events
from tracker.apps.tasks.models import Task

from .models import Activity

handlers = events.Handlers("activity")


async def record(task: Task, kind: str) -> None:
    await Activity.acreate(member_id=task.owner_id, task_id=task.id, kind=kind)


@handlers.in_transaction(events.Created[Task])
async def task_created(event: events.Created[Task]) -> None:
    await record(event.instance, "created")


@handlers.in_transaction(events.Updated[Task])
async def task_updated(event: events.Updated[Task]) -> None:
    await record(event.instance, "updated")
```

`in_transaction` runs the handler inside the transaction of the change, so the
task and its activity line commit together or not at all. The handlers are
registered by the `events` entry of the `activity` app in `hinoki.toml`
([chapter 7](https://hinoki.caspel.dev/dev/tutorial/6-run.md)), and one command lists them:

```sh
uv run hinoki events
```

```text
Created[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_created
Updated[Task]  in_transaction  activity     async  tracker.apps.activity.events.task_updated

limits: events.handler_timeout=5.0, events.max_handlers=16, events.max_depth=4
declared in hinoki.toml: activity -> tracker.apps.activity.events:handlers
```

The dependency points from `activity` to `tasks` only, which is also why
`activity` lists `requires = ["tasks", "members"]`. [Chapter 6](https://hinoki.caspel.dev/dev/tutorial/5-rules.md)
makes the other direction a failing check. See [events](https://hinoki.caspel.dev/dev/events.md) for
`after_commit` handlers and the limits.

## Recap

- An app announces what happened; another app listens without an import in the
  other direction.
- `in_transaction` keeps the main write and its audit trail atomic.
- `hinoki events` inspects the handlers declared in `hinoki.toml`.

Next: [6. Rules](https://hinoki.caspel.dev/dev/tutorial/5-rules.md).
