# Installation

Hinoki is pre-alpha and is not published on PyPI. Install it from its public repository with [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain:

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked
```

`uv sync` builds the native extension and installs the development tools.

## Create a project

The CLI creates a small app with typed settings, default rules, tests and agent instructions:

```sh
uv run hinoki new bookmarks --dir ..
cd ../bookmarks
uv sync
uv run pytest
```

The generated project includes a health route and a development MCP configuration. Review `AGENTS.md` for its project commands and conventions.

Continue with the [Quick start](QUICK-START.md) to run the Notes API, create its table with a reviewed migration and make a request.
