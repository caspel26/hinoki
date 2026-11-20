---
status: implemented
related:
  - patterns.md
  - quick-start.md
---

# Tutorial

Follow the Tracker, an API where members keep their own tasks, one layer per
chapter. Each chapter walks through files of the checked-in example in
[`examples/tracker`](https://github.com/caspel26/hinoki/tree/main/examples/tracker)
and runs its commands, so every step runs as shown. Chapter 8 then adds an app
to your own project.

| Chapter | It covers | You learn |
| --- | --- | --- |
| [1. Models](1-models.md) | tables, an index, a default order | models and migrations |
| [2. Schemas](2-schemas.md) | what clients send and receive | strict validation, derived schemas |
| [3. Operations](3-operations.md) | the business rules | ownership inside the statement |
| [4. Endpoints](4-endpoints.md) | the HTTP layer | access, transactions, declared errors |
| [5. Rules](5-rules.md) | checks that keep the layers apart | `hinoki check` |
| [6. Run and test it](6-run.md) | settings, a server, tests | `hinoki dev`, the test client |
| [7. Validate input](7-validation.md) | normalizers and checks | field and cross-field errors |
| [8. Projects and apps](8-projects.md) | an app in your own project | `hinoki app create`, planned migrations |

Chapters 1–6 cover the Tracker. Chapter 7 uses standalone examples, and
chapter 8 continues the project from the [Quick start](../quick-start.md).

The Tracker is one project (`tracker/`: `main.py`, settings, rules) with one app
(`tracker/apps/tracker/`) that owns the models, schemas, operations, security and
endpoints, created the way chapter 8 creates yours. Its layers:

```text
api.py          HTTP: shapes, status codes, access, transactions
   ↓
operations.py   business rules: plain async functions, no HTTP types
   ↓
models.py       tables and relations
```

## Before you start

[Install Hinoki](../getting-started.md). Chapters 1–6 run in the checkout's
`examples/` directory:

```sh
cd examples
```

That directory hosts three applications (Notes, Tracker and Blog) under one
`hinoki.toml`, so server and check commands name the application, such as
`uv run hinoki check tracker.main:app`. Without it, `hinoki check` here checks
the directory's own `project.app:app`, which declares no rules, and reports
`0 rule(s)`. In your own project the commands take no target.
