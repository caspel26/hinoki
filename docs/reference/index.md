# API reference

Reference for Hinoki's public Python surface. Public names are imported from `hinoki` unless a package is shown. The [user guides](../guides.md) explain common workflows; this section lists signatures and API details.

| Area | Package | Reference |
| --- | --- | --- |
| Application, routes, HTTP and middleware | `hinoki` / `hinoki.project` / `hinoki.http` | [Core API](web.md) |
| Generated model-backed routes and their filters | `hinoki.resources` / `hinoki.filters` | [CRUD](crud.md) |
| Validation and serialization | `hinoki` | [Schemas](schemas.md) |
| Models, columns and queries | `hinoki.orm` | [ORM](orm.md) |
| Parsers and response rendering | `hinoki.parsers` / `hinoki.renderers` | [Codecs](codecs.md) |
| Authentication and cryptography | `hinoki` / `hinoki.crypto` | [Auth](auth.md) |
| Database and transactions | `hinoki.db` | [Database](database.md) |
| Typed configuration | `hinoki` | [Settings](settings.md) |
| Rules and diagnostics | `hinoki.rules` | [Rules](rules.md) |
| Modules, events and runtime | `hinoki.modules` / `hinoki.events` | [Runtime](runtime.md) |
| Outbound email | `hinoki.mail` | [Email](email.md) |
| MCP and project scaffold | `hinoki.ai.mcpapp` / `hinoki.ai.mcp` | [Tools](tools.md) |
| Admin | `hinoki.admin` | [Admin](admin.md) |
| Tests | `hinoki.testing` | [Testing](testing.md) |
| Developer helpers | `hinoki.utils` | [Utilities](utilities.md) |

Signatures are generated from the installed source tree when the docs build. The framework is pre-alpha; check the source revision used by your checkout. Use the Guides menu for task-oriented explanations.
