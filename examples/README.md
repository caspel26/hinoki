# Hinoki examples

Three self-contained projects, in the order to read them. Each has its own
`hinoki.toml`, its own tests and a README with a reading order; run every command
from the example's directory, where `uv` finds the repository's environment.

| Level | Example | One purpose | Application |
| --- | --- | --- | --- |
| Easy | [Notes](notes/README.md) | An API in five minutes: one app, one model, generated CRUD | `notes.main:app` |
| Medium | [Tracker](tracker/README.md) | A team project: several apps with explicit dependencies, access policies, transactions, a filtered list, events, project-wide middleware and rules | `tracker.main:app` |
| Complete | [Blog](blog/README.md) | Production wiring: tenant, login, admin, MCP, migrations, streaming, events, a rule waiver, metrics | `blog.main:app` |

```sh
cd notes   # or tracker, or blog
uv run hinoki migrations apply
uv run hinoki dev
```

Each example's tests run with the repository's `uv run pytest`, or alone with
`uv run pytest examples/<name>/<name>_tests`.
