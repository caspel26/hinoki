# Project status

Hinoki is pre-alpha. The public API and migration format may change; it is not published on PyPI. Use a checkout for evaluation and test against disposable data.

## Implemented and exercised

The repository includes typed function and class APIs; request binding and strict schemas; access policies and declared errors; optional architecture rules; settings and worker modules; PostgreSQL models, relations, querysets and file-based migrations; sync and async handlers; OpenAPI, parsers, renderers, development and application MCP; and an optional admin with three styles.

The examples are the best way to see the pieces together: [Notes](https://github.com/caspel26/hinoki/tree/main/examples/notes) for a small declared app, [Tracker](https://github.com/caspel26/hinoki/tree/main/examples/tracker) for layers, ownership and rules, and [Blog](https://github.com/caspel26/hinoki/tree/main/examples/blog) for relations, auth and admin.

## Current boundaries

* PostgreSQL is the supported ORM database.
* Hinoki does not open database connections at import or apply migrations automatically.
* The ASGI path uses Python for server adaptation and handler calls; selected work is implemented in Rust.
* Generated typing for some keyword-based ORM calls is less precise than model construction; see [typing](TYPING.md).
* Advanced relation paths, implicit many-to-many, additional database backends, and end-to-end performance claims are not available.

See the [roadmap](https://github.com/caspel26/hinoki/blob/main/ROADMAP.md) for project direction and [contributing](https://github.com/caspel26/hinoki/blob/main/CONTRIBUTING.md) for local checks.
