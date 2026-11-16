# Tutorial: a task tracker, layer by layer

Chapters 1–6 follow the Tracker in [`examples/tracker`](https://github.com/caspel26/hinoki/tree/main/examples/tracker): members keep their own tasks, complete them, and a few rules stop it from rotting. Chapter 7 uses standalone validation examples. Chapter 8 starts a fresh project with the scaffold and app commands.

| Step | You add | You learn |
| --- | --- | --- |
| [1. Models](1-models.md) | tables, an index, a default order | how a model and a migration relate |
| [2. Schemas](2-schemas.md) | what clients send and see | strict validation, schemas derived from models |
| [3. Operations](3-operations.md) | the business rules | row permission inside the statement |
| [4. Endpoints](4-endpoints.md) | the HTTP layer | access, transactions, declared errors |
| [5. Rules](5-rules.md) | checks that keep the layers apart | `hinoki check`, import boundaries |
| [6. Run and test it](6-run.md) | settings, serving, tests | `dev`, `serve`, the test client |
| [7. Validate input](7-validation.md) | Schema normalization/checks and Model write validators | canonical fields, field-level and cross-field errors |
| [8. Projects and apps](8-projects.md) | `hinoki new`, modular apps and migration commands | generated project manifest, automatic migration names and local SQLite setup |

The Tracker chapters build and run an existing, versioned example. The validation chapter uses focused examples; the final chapter separately walks through creating a project and app, where Hinoki writes the manifest and discovers migrations automatically.

The shape you end with:

```
api.py          HTTP: shapes, status codes, access, transactions
   ↓
operations.py   business rules, plain async functions, no HTTP types
   ↓
models.py       tables and relations
```

Prerequisites: [install](../getting-started.md) from a checkout. A disposable PostgreSQL configured as `HINOKI_TEST_DATABASE_URL` is needed to run the database-backed Tracker tests in chapters 1–6. The standalone validation examples in chapter 7 need no database; chapter 8 uses local SQLite by default.
