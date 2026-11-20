---
status: implemented
related:
  - patterns.md
  - quick-start.md
---

# Tutorial

Build the Tracker, an API where members keep their own tasks, one layer per
chapter. The code is the checked-in example in
[`examples/tracker`](https://github.com/caspel26/hinoki/tree/main/examples/tracker),
so every step runs as shown.

| Chapter | You add | You learn |
| --- | --- | --- |
| [1. Models](1-models.md) | tables, an index, a default order | models and migrations |
| [2. Schemas](2-schemas.md) | what clients send and receive | strict validation, derived schemas |
| [3. Operations](3-operations.md) | the business rules | ownership inside the statement |
| [4. Endpoints](4-endpoints.md) | the HTTP layer | access, transactions, declared errors |
| [5. Rules](5-rules.md) | checks that keep the layers apart | `hinoki check` |
| [6. Run and test it](6-run.md) | settings, a server, tests | `hinoki dev`, the test client |
| [7. Validate input](7-validation.md) | normalizers and checks | field and cross-field errors |
| [8. Projects and apps](8-projects.md) | an app in your own project | `hinoki app create`, planned migrations |

Chapters 1–6 build the Tracker. Chapter 7 uses standalone examples, and
chapter 8 continues the project from the [Quick start](../quick-start.md).

The shape you end with:

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
`tracker.app:create_app`. In your own project they take no target.
