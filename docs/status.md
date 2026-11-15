# Project status

Hinoki is pre-alpha. The public API and migration format may change; this repository is not published on PyPI. The [`hinoki` project name](https://pypi.org/project/hinoki/) currently belongs to an unrelated legacy package. Use this repository's source checkout for evaluation and test against disposable data.

The [documentation site](documentation-versions.md) separates current development docs from release-tagged versions. Until the first release, the site root points to the `dev` docs.

## Implemented and exercised

The repository includes typed function and class APIs, optional model-backed CRUD route generation, request binding and strict schemas, built-in and custom Schema validation, opt-in Model write constraints and validators, access policies and declared errors, optional architecture rules, settings and worker modules, SQLite, PostgreSQL, MySQL and MariaDB ORM backends with relations, querysets and file-based migrations, sync and async handlers, OpenAPI, parsers, renderers, development and application MCP, and an optional admin with four styles.

The examples are the best way to see the pieces together: [Notes](https://github.com/caspel26/hinoki/tree/main/examples/notes) for a small declared app, [Tracker](https://github.com/caspel26/hinoki/tree/main/examples/tracker) for layers, ownership and rules, and [Blog](https://github.com/caspel26/hinoki/tree/main/examples/blog) for relations, auth and admin.

## Current boundaries

* SQLite, PostgreSQL, MySQL and MariaDB are supported ORM databases; SQLite is the local-development default. MySQL and MariaDB have separate real-server integration coverage in CI. SQL Server and Oracle are not supported. See the [database guide](databases.md) for backend-specific TLS, migration and feature limits, including MariaDB JSON behavior.
* Hinoki does not open database connections at import or apply migrations automatically.
* The ASGI adapter and application handler calls run in Python. The Rust extension currently provides path matching, selected query/header/cookie parsing, schema validation/serialization, cryptography and SQL ORM compilation/execution.
* Built-in security settings add browser response headers, HSTS on effective HTTPS requests, and an opt-in `308` HTTPS redirect that requires explicit allowed hosts.
* Generated typing for some keyword-based ORM calls is less precise than model construction; see [typing](typing.md).
* Many-to-many links use an explicit model with two foreign keys and a composite uniqueness constraint. Implicit join models, direct collection access such as `post.tags`, and advanced relation paths are not available.
* End-to-end performance claims are not made without comparable retained measurements; see the [benchmark report](benchmarks.md) and its limits.
* The server adapter is ASGI. `hinoki.wsgi.WSGIAdapter` wraps an application for WSGI servers (buffered bodies, no streaming); verified with Granian only, see [serving](serving.md#wsgi).
* CORS is one application-wide policy in the `cors` settings group; there is no per-route CORS option. Application MCP has its own allowed-origin check. Cursor pagination is not available; pagination currently uses page numbers.
* Pagination page-size defaults are `20` and `100`; the `pagination` settings group changes them for the whole app and an action's own `PageNumberPagination` values win. See [pagination](pagination.md).
* OpenAPI generation creates operation IDs and reads summary/description from handler docstrings. Per-action `tags`, `deprecated`, custom `operation_id` and `include_in_schema` controls are not available.
* There is no maintained Docker image or deployment recipe. `hinoki serve` runs the app with Granian; container packaging and process orchestration are application deployment choices.

See the [roadmap](https://github.com/caspel26/hinoki/blob/main/ROADMAP.md) for project direction and [contributing](https://github.com/caspel26/hinoki/blob/main/CONTRIBUTING.md) for local checks.
