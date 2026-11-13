# Getting started

Hinoki is pre-alpha and not on PyPI: work from a checkout.

## Install

```sh
git clone https://github.com/caspel26/hinoki && cd hinoki
uv sync --locked            # builds the Rust extension and installs the dev tools
```

You need [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain.

## Create a project

```sh
uv run hinoki new bookmarks --dir ..
cd ../bookmarks && uv sync && uv run pytest
```

The generated project has a health route, typed settings, rules that are on from day one, an `AGENTS.md` for AI agents and a `.mcp.json` for the development MCP server.

## A whole API in one file

`examples/notes/app.py` in the repository is a complete, tested API: a model, two `Schema` classes, four actions, a database module.

```python
@get("/notes/{note_id}")
async def one(note_id: UUID) -> NoteOut:
    return NoteOut.from_model(await Note.aget(id=note_id))


@post("/notes", status=201, transaction=True)
async def create(data: NoteIn) -> NoteOut:
    return NoteOut.from_model(await Note.acreate(data))
```

Path, query and body values are typed parameters; the `Request` object is optional (declare `request: Request` first when you need headers, cookies or the raw body). `Note.acreate(data)` takes the validated schema directly, and extra keywords (`acreate(data, owner_id=me)`) are set by the server and always win over the client's values.

## Run it

```sh
export NOTES_DATABASE_URL=postgres://postgres:pw@127.0.0.1:5432/notes
uv run hinoki dev examples.notes.app:create_app
```

Open `/docs` (Swagger UI) or `/redoc`.

## What next

* Structure a real application: [Patterns and best practices](PATTERNS.md) and the layered `examples/tracker`.
* Turn on the [rules](RULES.md) and configure with [settings](SETTINGS-DESIGN.md).
* Let an agent help: [Build with an agent](AI-WORKFLOW.md).
