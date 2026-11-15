# Installation

Hinoki is pre-alpha and this repository is not published on PyPI. The [`hinoki` project name](https://pypi.org/project/hinoki/) currently resolves to an unrelated legacy package. Install this repository from source with [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain:

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

The generated project includes a health route, a test that needs no database, a `README.md` with its commands and a development MCP configuration. Review `AGENTS.md` for its project commands and conventions. `pyproject.toml` names this repository as the source for `uv`, so it does not install the unrelated package by name; see [`hinoki new`](cli.md#new) to choose a checkout or a git revision with `--hinoki`.

Continue with the [Quick start](quick-start.md) to run the Notes API, create its table with a reviewed migration and make a request.
