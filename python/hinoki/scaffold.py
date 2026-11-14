"""``hinoki new NAME``: a small project an AI agent can work in.

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
dependencies = ["hinoki[server]"]

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
python_files = ["*test.py"]

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "B", "UP"]

[tool.ruff.lint.isort]
known-first-party = ["{name}"]
{sources}"""

MAIN = '''"""The application, declared: ``uv run hinoki dev {name}.main:app``."""

from hinoki import Hinoki

from {name}.api import health
from {name}.rules import project_rules
from {name}.settings import AppSettings

app = Hinoki(
    settings=AppSettings,  # {upper}_* variables and .env, declared on the class
    title="{title}",
    openapi_url="/openapi.json",
    docs_url="/docs",
    rules=project_rules,
    include=[health],
)
'''

SETTINGS = '''"""Typed settings. Built-in groups (execution, lifecycle, logging, docs) come from
the framework; add your own fields here and read them from ``app.settings``.

    {upper}_LOGGING__FORMAT=json   {upper}_EXECUTION__SYNC_WORKERS=16
    {upper}_DATABASE__URL=...      (after you add: database: DatabaseSettings)
"""

from hinoki import FrameworkSettings


class AppSettings(FrameworkSettings, prefix="{upper}_", dotenv=".env"):
    pass
'''

API = '''"""Endpoints. Protected actions name an Access policy; declare `request: Request` first only when you need it."""

from hinoki import Schema, http


class Health(Schema):
    status: str


@http.get("/health")
def health() -> Health:
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
    Actions.forbid_model_responses(match="{name}.*"),
)
'''

TEST = """from hinoki.testing import ASGIClient

from {name}.main import app


async def test_health_answers_through_the_whole_stack() -> None:
    async with ASGIClient(app) as client:
        reply = await client.get("/health")
    assert (reply.status, reply.body) == (200, {{"status": "ok"}})
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
        "run", "hinoki", "mcp",
        "{name}.main:app",
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
uv run hinoki check {name}.main:app --rules {name}.rules:project_rules --package {name}
uv run hinoki dev {name}.main:app      # reload; `serve` runs it for production
```

`check` and the MCP server build the app without opening a database, but settings are still loaded: once you add a required field such as `database: DatabaseSettings`, run them with a dummy value (`{upper}_DATABASE__URL=postgres://unused uv run hinoki check ...`, and add it under `env` in `.mcp.json`). Tests that touch the database need a real disposable one in `HINOKI_TEST_DATABASE_URL`.

`check` runs the project's rules and prints diagnostics with stable IDs
(`HNK-ACC-001` access, `HNK-TXN-001` transactions, `HNK-SCH-001` exposed fields,
`HNK-IMP-001` import boundaries). A violation is a failure to fix, not to hide.

## Conventions

* Path, query and body values are typed parameters. The request object is optional: declare `request: Request` as the first parameter only when you need headers, cookies or the raw body.
* Public by default: any action that is not public on purpose names an `Access` policy (`access=editor`).
* Writes use `transaction=True`. Failures are exceptions, not `Reply` values (a `Reply` commits).
* Request and response bodies are `Schema` classes. Never return a `Model`; list the fields a response may contain. Fields named in the exposure rule (`password_hash`, ...) never appear in a response schema.
* Relations are loaded explicitly (`select_related` / `prefetch_related`); there is no lazy loading.
* Create from a schema with `Model.acreate(data, owner_id=me)`: keyword values set by the server win over the client's. Build responses with `Out.from_model(row)`.
* Schema changes go through migrations (`hinoki migrations plan ...`), reviewed as SQL. Never apply them at startup.
* Configuration is typed settings (`{name}/settings.py`, loaded from `{upper}_*` environment variables); add fields there instead of reading `os.environ` in code. Secrets are `Secret` fields.
* No underscores in file names; tests are `*test.py`.

## Adding a database and a protected resource

The generated app has neither. The reference is `examples/tracker` in the framework repository; the shape is:

1. `models.py`: a `Member` (id, name, token) and your model with `owner_id: UUID = foreign_key(Member, on_delete="restrict", related_name="...")`.
2. `schemas.py`: an `In` schema without the owner, an `Out` schema.
3. `operations.py`: plain async functions taking `owner_id` and values; row permission goes in the statement (`Model.filter(owner_id=me)`, `aupdate_where({{...}})`), so someone else's row looks missing (404).
4. `security.py`: a `BearerAuth` that resolves a token to `Caller(id=..., user=member, permissions=...)` and an `Access(...)`.
5. `api.py`: actions with `access=...`, `caller: Caller[Member]`, `transaction=True` on writes, `pagination=` on lists; `main.py`: `modules=[DatabaseModule]` in `Hinoki(...)`, which reads `database.url` from the settings.
6. `hinoki migrations plan --models {name}.models`, then review the SQL.
7. Tests: `from hinoki.testing import ASGIClient`; `async with ASGIClient(app) as client: await client.post("/x", json={{...}}, token="...")`.

## What you may not do

* Do not edit `hinoki-rules.toml`, widen a rule's `except_`, or disable a rule so that a check passes. Those are the owner's decisions. If a rule seems wrong, say so and stop.
* Do not put secrets, tokens or real personal data in code, tests or fixtures.
* Do not run migrations or destructive SQL against anything but a disposable database.

## The development MCP

`.mcp.json` starts `hinoki mcp`: read-only tools `inspect_project`, `list_rules`, `explain_action`, `check_project`, `explain_error` (error codes and rule ids) and `propose_model` (starting-point source, never written for you; review it: add ownership scoping and pagination). Use them to see the real registry and rule results instead of guessing from files. They cannot change code, rules or waivers.

## Before you finish

Run the tests, ruff, mypy and `check`. Report what you ran and its result, what you did not run, and anything you could not verify.
"""


def framework_source() -> str:
    """A ``[tool.uv.sources]`` block pointing at this checkout when Hinoki is
    run from a source tree (it is not on PyPI; an unrelated ``hinoki`` is)."""
    import hinoki  # noqa: PLC0415 - only needed to find the checkout

    root = Path(hinoki.__file__).resolve().parents[2]
    if (root / "crates").is_dir() and (root / "pyproject.toml").is_file():
        return f'\n[tool.uv.sources]\nhinoki = {{ path = "{root}", editable = true }}\n'
    return ""


class ScaffoldError(ValueError):
    """The project cannot be created as asked."""


def files(name: str) -> dict[str, str]:
    """Relative path to content for a new project called ``name``."""
    title = name.capitalize()
    values = {
        "name": name,
        "title": title,
        "upper": name.upper(),
        "sources": framework_source(),
    }
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
        f"{name}/settings.py": SETTINGS.format(**values),
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
