# API reference

Reference for Hinoki's public Python surface. Public names are imported from `hinoki` unless a package is shown. The [user guides](https://hinoki.caspel.dev/dev/guides.md) explain common workflows; this section lists signatures and API details.

| Area | Package | Reference |
| --- | --- | --- |
| Application, routes, HTTP and middleware | `hinoki` / `hinoki.project` / `hinoki.http` | [Core API](https://hinoki.caspel.dev/dev/reference/web.md) |
| Generated model-backed routes and their filters | `hinoki.resources` / `hinoki.filters` | [CRUD](https://hinoki.caspel.dev/dev/reference/crud.md) |
| Validation and serialization | `hinoki` | [Schemas](https://hinoki.caspel.dev/dev/reference/schemas.md) |
| Models, columns and queries | `hinoki.orm` | [ORM](https://hinoki.caspel.dev/dev/reference/orm.md) |
| Parsers and response rendering | `hinoki.parsers` / `hinoki.renderers` | [Codecs](https://hinoki.caspel.dev/dev/reference/codecs.md) |
| Authentication and cryptography | `hinoki` / `hinoki.crypto` | [Auth](https://hinoki.caspel.dev/dev/reference/auth.md) |
| Database and transactions | `hinoki.db` | [Database](https://hinoki.caspel.dev/dev/reference/database.md) |
| Typed configuration | `hinoki` | [Settings](https://hinoki.caspel.dev/dev/reference/settings.md) |
| Rules and diagnostics | `hinoki.rules` | [Rules](https://hinoki.caspel.dev/dev/reference/rules.md) |
| Modules, events and runtime | `hinoki.modules` / `hinoki.events` | [Runtime](https://hinoki.caspel.dev/dev/reference/runtime.md) |
| Outbound email | `hinoki.mail` | [Email](https://hinoki.caspel.dev/dev/reference/email.md) |
| Application sessions | `hinoki.sessions` | [Sessions](https://hinoki.caspel.dev/dev/reference/sessions.md) |
| Cache | `hinoki.cache` | [Cache](https://hinoki.caspel.dev/dev/reference/cache.md) |
| Uploaded file storage | `hinoki.storage` | [File storage](https://hinoki.caspel.dev/dev/reference/storage.md) |
| MCP and project scaffold | `hinoki.ai.mcpapp` / `hinoki.ai.mcp` | [Tools](https://hinoki.caspel.dev/dev/reference/tools.md) |
| Admin | `hinoki.admin` | [Admin](https://hinoki.caspel.dev/dev/reference/admin.md) |
| Tests | `hinoki.testing` | [Testing](https://hinoki.caspel.dev/dev/reference/testing.md) |
| Developer helpers | `hinoki.utils` | [Utilities](https://hinoki.caspel.dev/dev/reference/utilities.md) |

Signatures are generated from the installed source tree when the docs build. The framework is pre-alpha; check the source revision used by your checkout. Use the Guides menu for task-oriented explanations.
