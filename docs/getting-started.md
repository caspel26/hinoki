---
status: implemented
related:
  - quick-start.md
  - cli.md
---

# Installation

Hinoki is pre-alpha and installs from a source checkout. You need:

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) 0.9 or newer
- a stable Rust toolchain from [rustup](https://rustup.rs/)

## Install from source

```sh
git clone https://github.com/caspel26/hinoki
cd hinoki
uv sync --locked
uv run hinoki version
```

`uv sync --locked` builds the Rust extension and installs the development tools.
The last command confirms the installation:

```text
hinoki 0.0.1
```

!!! warning "Not on PyPI"
    The [`hinoki` name on PyPI](https://pypi.org/project/hinoki/) belongs to an
    unrelated legacy package: do not install Hinoki by name. A project created
    with `hinoki new` points `uv` at this checkout in `pyproject.toml`
    (`[tool.uv.sources]`), so `uv sync` never fetches that package.

Next: the [Quick start](quick-start.md) creates and serves a project.
