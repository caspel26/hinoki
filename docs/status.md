# Project status

Hinoki is pre-alpha. The public API and migration format may change; it is not published on PyPI. Use a checkout for evaluation and test against disposable data.

## Implemented and exercised

The repository includes typed function and class APIs; request binding and strict schemas; access policies and declared errors; optional architecture rules; settings and worker modules; PostgreSQL models, relations, querysets and file-based migrations; sync and async handlers; OpenAPI, parsers, renderers, development and application MCP; and an optional admin with three styles.

The examples are the best way to see the pieces together: [Notes](https://github.com/caspel26/hinoki/tree/main/examples/notes) for a small declared app, [Tracker](https://github.com/caspel26/hinoki/tree/main/examples/tracker) for layers, ownership and rules, and [Blog](https://github.com/caspel26/hinoki/tree/main/examples/blog) for relations, auth and admin.

## Current boundaries

* PostgreSQL is the supported ORM database.
* Hinoki does not open database connections at import or apply migrations automatically.
* The ASGI path uses Python for server adaptation and handler calls; selected work is implemented in Rust.
* Generated typing for some keyword-based ORM calls is less precise than model construction; see [typing](typing.md).
* Many-to-many links use an explicit model with two foreign keys and a composite uniqueness constraint. Implicit join models, direct collection access such as `post.tags`, and advanced relation paths are not available.
* Additional database backends and end-to-end performance claims are not available.
* The server adapter is ASGI. WSGI support is not available.
* General CORS middleware is not available; application MCP has its own allowed-origin check. Cursor pagination is not available; pagination currently uses page numbers.
* Pagination page-size defaults are `20` and `100` on `PageNumberPagination`; app-wide settings for overriding them are not available yet.
* OpenAPI generation creates operation IDs and reads summary/description from handler docstrings. Per-action `tags`, `deprecated`, custom `operation_id` and `include_in_schema` controls are not available.
* There is no maintained Docker image or deployment recipe. `hinoki serve` runs the app with Granian; container packaging and process orchestration are application deployment choices.

See the [roadmap](https://github.com/caspel26/hinoki/blob/main/ROADMAP.md) for project direction and [contributing](https://github.com/caspel26/hinoki/blob/main/CONTRIBUTING.md) for local checks.
