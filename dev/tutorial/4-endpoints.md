# 4. Endpoints

The HTTP layer maps requests to operations and results to responses. It holds
no business rule and no SQL.

First, who may call. A bearer token resolves to a `Member` through its SHA-256 digest (the table never holds the token), and the policy
names the permission that every task action requires. The policy lives in the
members app, so every app reuses it:

```python
# tracker/apps/members/security.py
"""Authentication: an opaque bearer token resolved to a ``Member``."""

import hashlib

from hinoki import Access, BearerAuth, Caller, Permission

from .models import Member


def hash_token(token: str) -> str:
    """The SHA-256 hex digest stored instead of the token (high-entropy, so no KDF)."""
    return hashlib.sha256(token.encode()).hexdigest()


class MemberPermission(Permission):
    USE = "tracker:use"


class MemberTokens(BearerAuth[Member]):
    async def resolve_token(self, token: str) -> Caller[Member] | None:
        member = await Member.aget_or_none(token_hash=hash_token(token))
        if member is None:
            return None
        return Caller(
            id=str(member.id), user=member, permissions={MemberPermission.USE}
        )


member_only = Access(auth=MemberTokens(), permissions=[MemberPermission.USE])
```

Next, what a client may ask of the task list. A `FilterSet` declares each query
parameter once; only what it declares is accepted:

```python
# tracker/apps/tasks/filters.py
from datetime import datetime

from hinoki.filters import Eq, FilterSet, In, Range, Search


class TaskFilters(FilterSet):
    done: Eq[bool]
    priority: In[int]
    created_at: Range[datetime]
    completed_at: Eq[datetime | None]
    search = Search("title")
    ordering = ("created_at", "priority", "title")
```

Then the actions. List, retrieve, create and delete are generated from the
model by `@crud`; the one action it cannot know, `complete`, is handwritten in
the same class:

```python
# tracker/apps/tasks/api.py
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
```

The hooks `get_queryset` and `remove` take a typed `caller: Caller[Member]`.
That is valid because every generated action that calls them has the
`member_only` policy, so the caller is never `None`; Hinoki checks this when
the class is declared and raises `RegistrationError` otherwise. For a resource
that mixes public and protected actions, annotate `Caller[Member] | None`. See
[hooks](https://hinoki.caspel.dev/dev/crud.md#hooks-in-the-class-form) in the CRUD guide.

The project's `GET /health` is a plain function in `tracker/routes.py`, public
on purpose, and it answers a `Schema` like every other action:

```python
# tracker/routes.py
from hinoki import Includable, Schema, http


class Health(Schema):
    status: str


@http.get("/health")
def health() -> Health:
    return Health(status="ok")


routes: list[Includable] = [health]
```

- **`@crud(...)`** generates the list, retrieve, create and delete actions with
  the policy, the schemas and the filters you name. `owner="owner_id"` sets
  the owner of a created task to the caller, and `read=TaskOut` is the only
  shape that leaves the server. Writes of generated actions are transactional
  by default.
- **`get_queryset`** scopes every generated action to the caller, so another
  member's task is a 404. **`remove`** replaces the generated delete body with
  the operation, which keeps completed tasks.
- **`access=`** names a reusable policy. Actions are public unless they name
  one, so protect on purpose; a rule in the next chapter enforces it.
- **`atomic=True`** runs the handwritten write in a transaction. It commits
  after the response is encoded and rolls back before an error becomes HTTP.
- **`errors=(...)`** declares what an action may raise. The OpenAPI document,
  the MCP tool description and the strict test client all read it.
- **`caller: Caller[Member]`** is the authenticated caller, typed:
  `caller.user` is your `Member`.
- **`TaskOut.from_model(...)`** converts a row into the response. A handler
  never returns a model.

The list accepts exactly the filters of `TaskFilters`, next to the pagination
parameters:

```text
GET /tasks?done=false&priority=1,2&created_at.from=2026-01-01T00:00:00Z&search=docs&ordering=-created_at
```

A name, an operator or an ordering key that `TaskFilters` does not declare is a
`422` that lists every problem. See [filter, order and search the
list](https://hinoki.caspel.dev/dev/crud.md#filter-order-and-search-the-list).

The two smaller apps follow the same shape. `members` has one handwritten
action, `GET /members/me`, and `activity` has a read-only generated list scoped
to the caller:

```python
# tracker/apps/activity/api.py
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
```

Declare `request: Request` as the first parameter only when you need headers,
cookies or the raw body. File uploads are covered in
[upload files](https://hinoki.caspel.dev/dev/endpoints.md#upload-files).

## Register the routes

Nothing in `main.py` lists these routes. `hinoki.toml` points each app at its
`routes` list and gives it the prefix (`/tasks`, `/members`, `/activity`, by
default `/<app name>`), which is why `path=""` above repeats nothing
([chapter 6](https://hinoki.caspel.dev/dev/tutorial/6-run.md)).

## Recap

- `@crud` generates the standard actions from the model, the policy, the
  schemas and a `FilterSet`; a handwritten action sits beside them.
- Handlers call operations and convert rows with `from_model`.
- Every error an action raises is declared.

Next: [5. Rules](https://hinoki.caspel.dev/dev/tutorial/5-rules.md). In the guides: [function-based APIs](https://hinoki.caspel.dev/dev/function-endpoints.md) and [class-based APIs](https://hinoki.caspel.dev/dev/class-endpoints.md).
