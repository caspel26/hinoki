# Tutorial: a task tracker, layer by layer

You will build the tracker that lives in [`examples/tracker`](https://github.com/caspel26/hinoki/tree/main/examples/tracker): members keep their own tasks, complete them, and a few rules stop it from rotting. Each step is a real file from the repository, so everything here runs.

| Step | You add | You learn |
| --- | --- | --- |
| [1. Models](1-models.md) | tables, an index, a default order | how a model and a migration relate |
| [2. Schemas](2-schemas.md) | what clients send and see | strict validation, schemas derived from models |
| [3. Operations](3-operations.md) | the business rules | row permission inside the statement |
| [4. Endpoints](4-endpoints.md) | the HTTP layer | access, transactions, declared errors |
| [5. Rules](5-rules.md) | checks that keep the layers apart | `hinoki check`, import boundaries |
| [6. Run and test it](6-run.md) | settings, serving, tests | `dev`, `serve`, the test client |

The shape you end with:

```
api.py          HTTP: shapes, status codes, access, transactions
   ↓
operations.py   business rules, plain async functions, no HTTP types
   ↓
models.py       tables and relations
```

Prerequisites: [install](../getting-started.md) from a checkout and have a disposable PostgreSQL (`HINOKI_TEST_DATABASE_URL` in the examples).
