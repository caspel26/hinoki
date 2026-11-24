# Tutorial

Follow the Tracker, an API where members keep their own tasks, one layer per
chapter. Each chapter walks through files of the checked-in example in
[`examples/tracker`](https://github.com/caspel26/hinoki/tree/main/examples/tracker)
and runs its commands, so every step runs as shown. Chapter 8 then adds an app
to your own project.

| Chapter | It covers | You learn |
| --- | --- | --- |
| [1. Models](https://hinoki.caspel.dev/dev/tutorial/1-models.md) | tables, a foreign key, an index, a default order | models and migrations |
| [2. Schemas](https://hinoki.caspel.dev/dev/tutorial/2-schemas.md) | what clients send and receive | strict validation, derived schemas |
| [3. Operations](https://hinoki.caspel.dev/dev/tutorial/3-operations.md) | the business rules and the events they announce | ownership inside the statement, events between apps |
| [4. Endpoints](https://hinoki.caspel.dev/dev/tutorial/4-endpoints.md) | the HTTP layer | access, generated CRUD with filters, transactions, declared errors |
| [5. Rules](https://hinoki.caspel.dev/dev/tutorial/5-rules.md) | checks that keep the layers apart | `hinoki check` |
| [6. Run and test it](https://hinoki.caspel.dev/dev/tutorial/6-run.md) | the manifest, settings, a server, tests | `hinoki dev`, the test client |
| [7. Validate input](https://hinoki.caspel.dev/dev/tutorial/7-validation.md) | normalizers and checks | field and cross-field errors |
| [8. Projects and apps](https://hinoki.caspel.dev/dev/tutorial/8-projects.md) | an app in your own project | `hinoki app create`, planned migrations |

Chapters 1–6 cover the Tracker. Chapter 7 uses standalone examples, and
chapter 8 continues the project from the [Quick start](https://hinoki.caspel.dev/dev/quick-start.md).

## The Tracker's shape

The Tracker is one project with three apps that depend on each other on
purpose. Each app is a package with its own models, endpoints and migrations:

```text
tracker/
├── main.py, settings.py   the application and its typed settings
├── routes.py              routes that belong to no app (GET /health)
├── middleware.py          a project-wide middleware
├── rules.py               architecture rules
└── apps/
    ├── members/           Member, the bearer token and the access policy
    ├── tasks/             Task: models, schemas, filters, operations, api
    └── activity/          Activity, written by listening to task events
```

`hinoki.toml` is the whole wiring. `[project]` names the application, the
project routes, the middleware and the rule sets; each `[[apps]]` names its
models, migrations and routes, and `requires` fixes the order:

```toml
# examples/tracker/hinoki.toml
[project]
application = "tracker.main:app"
routes = "tracker.routes:routes"
middleware = ["tracker.middleware:middleware"]
rules = ["tracker.rules:policy", "tracker.rules:layers"]

[[apps]]
name = "members"
models = "tracker.apps.members.models"
migrations = "tracker.apps.members.migrations"
routes = "tracker.apps.members.api:routes"
requires = []

[[apps]]
name = "tasks"
models = "tracker.apps.tasks.models"
migrations = "tracker.apps.tasks.migrations"
routes = "tracker.apps.tasks.api:routes"
requires = ["members"]

[[apps]]
name = "activity"
models = "tracker.apps.activity.models"
migrations = "tracker.apps.activity.migrations"
routes = "tracker.apps.activity.api:routes"
events = "tracker.apps.activity.events:handlers"
requires = ["tasks", "members"]
```

Inside the `tasks` app, the layers point one way:

```text
api.py          HTTP: shapes, status codes, access, transactions
   ↓
operations.py   business rules: plain async functions, no HTTP types
   ↓
models.py       tables and relations
```

`activity` reacts to what `tasks` announces, and `tasks` never imports it
([chapter 3](https://hinoki.caspel.dev/dev/tutorial/3-operations.md) and [chapter 5](https://hinoki.caspel.dev/dev/tutorial/5-rules.md)).

## Before you start

[Install Hinoki](https://hinoki.caspel.dev/dev/getting-started.md). Chapters 1–6 run in the Tracker's own
directory of the checkout, which is a complete project with its own
`hinoki.toml`:

```sh
cd examples/tracker
```

Commands there take no target: `uv run hinoki check`, `uv run hinoki dev` and
`uv run hinoki migrations apply` read the manifest, as they do in your own
project. They create `examples/tracker/.hinoki/local.sqlite3`, which the
repository ignores.
