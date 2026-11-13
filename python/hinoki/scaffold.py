"""``python -m hinoki new NAME``: a small project an AI agent can work in.

The generated project has a health route, rules that are on from day one
(`HNK-ACC-001`, `HNK-TXN-001`, `HNK-SCH-001`), an `AGENTS.md` that tells an
agent the conventions and where its authority ends, a `.mcp.json` that starts
the read-only development MCP, and a test. Nothing is installed or executed.
"""

from __future__ import annotations

import re
from pathlib import Path

NAME = re.compile(r"[a-z][a-z0-9]*")

PYPROJECT = """[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["hinoki", "granian"]

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
python_files = ["*test.py"]
"""

MAIN = '''"""Application factory: ``uv run granian --interface asgi --factory {name}.main:create_app``."""

from hinoki import HinokiAPI

from {name}.api import health
from {name}.rules import project_rules


def create_app() -> HinokiAPI:
    app = HinokiAPI(
        title="{title}",
        openapi_url="/openapi.json",
        docs_url="/docs",
        rules=project_rules,
    )
    app.include(health)
    return app
'''

API = '''"""Endpoints. Every action takes the request first; protected actions name an Access policy."""

from hinoki import Request, Schema, get


class Health(Schema):
    status: str


@get("/health")
def health(request: Request) -> Health:
    """Liveness: answers while the worker runs."""
    return Health(status="ok")
'''

RULES = '''"""The project's rules. Weaken them only with the owner's decision (see hinoki-rules.toml)."""

from hinoki import Actions, Schemas, Transactions
from hinoki.rules import RuleSet

project_rules = RuleSet(
    Actions.require_access(match="{name}.*", except_=["{name}.api.health"]),
    Transactions.require_atomic(),
    Schemas.forbid_exposure("password_hash", "password", "token"),
)
'''

TEST = """from hinoki import HinokiAPI

from {name}.main import create_app


def test_the_app_builds_and_routes_health() -> None:
    app: HinokiAPI = create_app()
    found = app.resolve("GET", "/health")
    assert found is not None and found[0].name == "health"
"""

WAIVERS = """# Exceptions to the project's rules. This file is decided by the project owner:
# agents and tools read it but must not edit it to make a check pass.
#
# [[waiver]]
# rule = "HNK-ACC-001"
# target = "{name}.api.health"
# reason = "why this exception is acceptable"
# owner = "who accepts it"
# expires = 2027-01-31   # optional
"""

MCP = """{{
  "mcpServers": {{
    "hinoki": {{
      "command": "uv",
      "args": [
        "run", "python", "-m", "hinoki", "mcp",
        "--app", "{name}.main:create_app",
        "--rules", "{name}.rules:project_rules",
        "--package", "{name}"
      ]
    }}
  }}
}}
"""

CLAUDE = "Read and follow `AGENTS.md` before changing anything in this repository.\n"

AGENTS = """# Working on {title} with an AI agent

A Hinoki application. Keep changes small, typed and checked.

## Commands

```sh
uv sync
uv run pytest                                   # tests
uv run ruff check . && uv run ruff format .     # lint and format
uv run mypy {name}                              # types
uv run python -m hinoki check --app {name}.main:create_app --rules {name}.rules:project_rules --package {name}
uv run granian --interface asgi --factory {name}.main:create_app
```

`check` runs the project's rules and prints diagnostics with stable IDs
(`HNK-ACC-001` access, `HNK-TXN-001` transactions, `HNK-SCH-001` exposed fields,
`HNK-IMP-001` import boundaries). A violation is a failure to fix, not to hide.

## Conventions

* Every action takes `request: Request` first. Path, query and body values follow as typed parameters.
* Public by default: any action that is not public on purpose names an `Access` policy (`access=editor`).
* Writes use `transaction=True`. Failures are exceptions, not `Reply` values (a `Reply` commits).
* Request and response bodies are `Schema` classes. Never return a `Model`; list the fields a response may contain. Fields named in the exposure rule (`password_hash`, ...) never appear in a response schema.
* Relations are loaded explicitly (`select_related` / `prefetch_related`); there is no lazy loading.
* Create from a schema with `Model.acreate(data, owner_id=me)`: keyword values set by the server win over the client's. Build responses with `Out.from_model(row)`.
* Schema changes go through migrations (`python -m hinoki migrations plan ...`), reviewed as SQL. Never apply them at startup.
* No underscores in file names; tests are `*test.py`.

## What you may not do

* Do not edit `hinoki-rules.toml`, widen a rule's `except_`, or disable a rule so that a check passes. Those are the owner's decisions. If a rule seems wrong, say so and stop.
* Do not put secrets, tokens or real personal data in code, tests or fixtures.
* Do not run migrations or destructive SQL against anything but a disposable database.

## The development MCP

`.mcp.json` starts `python -m hinoki mcp`: read-only tools `inspect_project`, `list_rules`, `explain_action`, `check_project` and `explain_error`. Use them to see the real registry and rule results instead of guessing from files. They cannot change code, rules or waivers.

## Before you finish

Run the tests, ruff, mypy and `check`. Report what you ran and its result, what you did not run, and anything you could not verify.
"""


class ScaffoldError(ValueError):
    """The project cannot be created as asked."""


def files(name: str) -> dict[str, str]:
    """Relative path to content for a new project called ``name``."""
    title = name.capitalize()
    values = {"name": name, "title": title}
    return {
        "pyproject.toml": PYPROJECT.format(**values),
        "AGENTS.md": AGENTS.format(**values),
        "CLAUDE.md": CLAUDE,
        ".mcp.json": MCP.format(**values),
        "hinoki-rules.toml": WAIVERS.format(**values),
        f"{name}/__init__.py": "",
        f"{name}/main.py": MAIN.format(**values),
        f"{name}/api.py": API.format(**values),
        f"{name}/rules.py": RULES.format(**values),
        "tests/__init__.py": "",
        "tests/healthtest.py": TEST.format(**values),
        ".gitignore": ".venv/\n__pycache__/\n*.pyc\n.mypy_cache/\n.ruff_cache/\n",
    }


def create(name: str, parent: Path) -> Path:
    """Write the project into ``parent / name``; refuse a bad name or a target
    that exists and is not empty."""
    if not NAME.fullmatch(name):
        raise ScaffoldError(
            "the name must be lower-case letters and digits, starting with a letter "
            "(no underscores or dashes: it is also the package name)"
        )
    target = parent / name
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"{target} already exists and is not empty")
    for relative, content in files(name).items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target
