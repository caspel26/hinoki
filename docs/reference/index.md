# API reference

Reference for Hinoki's public Python surface. Public names are imported from `hinoki` unless a package is shown. The [user guides](../getting-started.md) explain common workflows; this section lists signatures and API details.

| Area | Package | Reference |
| --- | --- | --- |
| Application, routes and HTTP | `hinoki` | [Core API](web.md) |
| Validation and serialization | `hinoki` | [Schemas](schemas.md) |
| Models, columns and queries | `hinoki.orm` | [ORM](orm.md) |
| Parsers and response rendering | `hinoki.parsers` / `hinoki.renderers` | [Codecs](codecs.md) |
| Authentication and cryptography | `hinoki` / `hinoki.crypto` | [Auth](auth.md) |
| Database and transactions | `hinoki.db` | [Database](database.md) |
| Typed configuration | `hinoki` | [Settings](settings.md) |
| Rules and diagnostics | `hinoki.rules` | [Rules](rules.md) |
| Modules and runtime | `hinoki.modules` | [Runtime](runtime.md) |
| MCP and project scaffold | `hinoki.mcpapp` / `hinoki.mcp` | [Tools](tools.md) |
| Admin | `hinoki.admin` | [Admin](admin.md) |
| Tests | `hinoki.testing` | [Testing](testing.md) |

Signatures are generated from the installed source tree when the docs build. The framework is pre-alpha; check the source revision used by your checkout. Use the Guides menu for task-oriented explanations.
