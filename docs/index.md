---
title: Hinoki
hide:
  - navigation
  - toc
---

# Hinoki { .hn-hidden }

<div class="hn-hero" markdown>

![Hinoki](assets/hinoki-banner.png)

<p class="tag">Typed Python APIs with a native Rust core. Plain Python on the surface, verifiable architecture underneath.</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[Patterns](PATTERNS.md){ .md-button }
[GitHub](https://github.com/caspel26/hinoki){ .md-button }

</div>

!!! warning "Pre-alpha"
    Hinoki is not published to PyPI and its APIs will change. A reference blog and a layered tracker run end to end on PostgreSQL; do not build on it yet.

```python
from hinoki import Request, Schema, get, post
from hinoki.orm import Model, field


class Note(Model, table="notes"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=80)


class NoteIn(Schema):
    title: Annotated[str, Field(min_length=1, max_length=80)]


@post("/notes", status=201, transaction=True)
async def create(request: Request, data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))
```

<div class="hn-grid" markdown>

<div class="hn-card" markdown>
### Plain, typed Python
Functions or classes, sync or async, one declaration feeding routing, validation, policies and OpenAPI. Not built on Django, FastAPI, Starlette or Pydantic.
</div>

<div class="hn-card" markdown>
### A Rust core
HTTP parsing and rendering, schema validation and serialisation, the query compiler, Argon2id and JWT run in Rust through PyO3. [Sync and async](SYNC-ASYNC.md) are both first-class.
</div>

<div class="hn-card" markdown>
### Architecture you can check
Optional [rules](RULES.md) fail the build on a missing access policy, a write without a transaction, an exposed `password_hash`, or an import against the layering.
</div>

<div class="hn-card" markdown>
### Typed settings
[Immutable settings](SETTINGS-DESIGN.md) loaded from the environment, secrets that never print, every problem reported at once.
</div>

<div class="hn-card" markdown>
### Built for AI agents
A project generator with conventions, a [development MCP server](AI-WORKFLOW.md) and an [application MCP](APP-MCP-DESIGN.md) that serves your actions with the caller's own credentials.
</div>

<div class="hn-card" markdown>
### An admin that is yours
Three distinct themes on one component set: soft Studio, neon Midnight, editorial Paper. CSRF-safe, contrast-checked.
</div>

</div>

## Where to go next

* **New here:** [Getting started](getting-started.md), then [Patterns and best practices](PATTERNS.md).
* **Looking something up:** the [API reference](reference/index.md).
* **Why it is built this way:** the Design section and the [decision records](adr/0001-server-adapter-asgi-on-granian.md).
