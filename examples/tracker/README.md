# Tracker

The medium example: several apps that depend on each other on purpose. It adds
to [Notes](../notes/README.md) what a team project needs: explicit app
dependencies, access policies, transactions, a filtered generated list, events
between apps, a project-wide middleware and architecture rules. A member has a
bearer token and sees only their own tasks. There is no sign-in flow, tenant or
admin: Blog adds those.

## Read it in this order

| # | File | What to see |
| --- | --- | --- |
| 1 | [`hinoki.toml`](hinoki.toml) | The whole wiring. `[project]` lists the application, the project routes, the middleware and the rule sets (lines 1-5). Each `[[apps]]` names its models, migrations and routes; `requires` fixes the order (lines 12, 19 and 27) and `events` registers the handlers of `activity` (line 26) |
| 2 | [`tracker/apps/members/security.py`](tracker/apps/members/security.py) | `MemberTokens` resolves a bearer token to a `Member` by the SHA-256 digest of the token, which is all the table stores (line 19); the reusable `member_only` access policy (line 29) |
| 3 | [`tracker/apps/tasks/models.py`](tracker/apps/tasks/models.py) | `Task` belongs to a `Member` through a foreign key (lines 12-24) |
| 4 | [`tracker/apps/tasks/schemas.py`](tracker/apps/tasks/schemas.py) | The request body `TaskIn` (line 10) and the response `TaskOut` derived from the model without the owner (line 18) |
| 5 | [`tracker/apps/tasks/filters.py`](tracker/apps/tasks/filters.py) | The `FilterSet` of the list: `Eq`, `In`, `Range`, a `.null` filter, `Search` and `ordering` (lines 8-14) |
| 6 | [`tracker/apps/tasks/operations.py`](tracker/apps/tasks/operations.py) | The business rules as plain async functions with no HTTP types: `complete_task` (line 25) and `delete_task` (line 39). The owner is part of each statement; `complete_task` announces `Updated[Task]` itself (line 33) |
| 7 | [`tracker/apps/tasks/api.py`](tracker/apps/tasks/api.py) | `@crud` generates the list, retrieve, create and delete with the `access`, `owner` and `filters` options (lines 17-26); `get_queryset` scopes every action to the caller (line 30); `remove` calls the operation (line 33); `complete` is handwritten with `atomic=True` (lines 39-49) |
| 8 | [`tracker/apps/activity/events.py`](tracker/apps/activity/events.py) | `activity` listens to `Created[Task]` and `Updated[Task]` (lines 17 and 22) and writes in the same transaction; `tasks` never imports it |
| 9 | [`tracker/middleware.py`](tracker/middleware.py) | `ApiVersion` adds a header to every answer of a matched route (line 6) |
| 10 | [`tracker/rules.py`](tracker/rules.py) | Two `RuleSet` objects: `policy` (what an action must declare, line 14) and `layers` (who may import whom, line 27) |
| 11 | [`tracker/main.py`](tracker/main.py) | `Hinoki(...)` with no `include`, `events`, `middleware` or `rules`: all come from the manifest (line 14) |
| 12 | [`tracker/commands.py`](tracker/commands.py) | A management command, `hinoki manage seed` (line 23) |
| 13 | [`tracker_tests/`](tracker_tests/) | HTTP, filters, activity and project tests; the fixture in [`conftest.py`](tracker_tests/conftest.py) migrates each app into a SQLite file per test |

Each app has its own `migrations/`; review the SQL before applying it.

## Run

From this directory (`examples/tracker`):

```sh
uv run hinoki migrations apply
uv run hinoki manage seed   # prints the token; then: export TOKEN=<the printed token>
uv run hinoki dev
```

`migrations apply` creates `.hinoki/local.sqlite3` here and applies the apps in
`requires` order. `manage seed` creates the member `Ada Demo` and prints a random token once;
the table keeps only its digest. A local SQLite database created before tokens were stored as digests has the old `members` column: delete `.hinoki/local.sqlite3` and migrate again. Export the token and, in another terminal:

```sh
curl -X POST http://127.0.0.1:8000/tasks -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"title":"Write docs","priority":2}'
curl 'http://127.0.0.1:8000/tasks?done=false&priority=1,2&search=docs&ordering=-created_at' -H "authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/activity -H "authorization: Bearer $TOKEN"
```

| Route | Action |
| --- | --- |
| `GET /health` | liveness, public |
| `GET /members/me` | the member the token belongs to |
| `GET /tasks` | your tasks; filters `done`, `priority`, `created_at.from`/`.to`, `completed_at.null`, `search`, `ordering` |
| `GET /tasks/{item_id}` | one of your tasks, or 404 |
| `POST /tasks` | create a task; the owner is the caller |
| `POST /tasks/{item_id}/complete` | mark it done; completing twice is `409` |
| `DELETE /tasks/{item_id}` | delete an open task; a completed one answers `409` |
| `GET /activity` | what happened to your tasks |

Useful commands, all from this directory: `uv run hinoki routes`, `uv run
hinoki events` (the handlers declared in `hinoki.toml`), `uv run hinoki check`
(the 14 rules) and `uv run hinoki migrations plan --check`.

## Test

From the repository root:

```sh
uv run --locked pytest examples/tracker/tracker_tests
```

No environment variable is needed: each test migrates its own SQLite file in a
temporary directory and talks to the application with `ASGIClient`.
