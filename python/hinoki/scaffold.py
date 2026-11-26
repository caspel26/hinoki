"""``hinoki new NAME``: a small project an AI agent can work in.

The generated project has a health route, eight rules that are on from day one
(access, permissions, response types, pagination, transactions, exposed fields
and request string limits; see ``RULES``), an `AGENTS.md` that tells an
agent the conventions and where its authority ends, a `.mcp.json` that starts
the read-only development MCP, and a test. Nothing is installed or executed.
"""

from __future__ import annotations

import keyword
import re
import sys
from pathlib import Path

from hinoki import jsontext
from hinoki.ai.skills import skill_files

NAME = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
RESERVED_NAMES = sys.stdlib_module_names | set(keyword.kwlist) | {"hinoki"}

PYPROJECT = """[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["hinoki-api[server]"]

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

MAIN = '''"""The application. `hinoki.toml` names it; `uv run hinoki dev` serves it."""

from hinoki import Hinoki

from {name}.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,  # {upper}_* variables and .env
    title="{title}",
    openapi_url="/openapi.json",
    docs_url="/docs",
)  # routes, rules and middleware come from hinoki.toml
'''

ROUTES = '''"""The project's route map. Mount apps explicitly with Mount("/<prefix>", ...)."""

from hinoki import Includable

from {name}.api import health

routes: list[Includable] = [health]
'''

SETTINGS = '''"""Typed settings. Built-in groups (execution, lifecycle, logging, docs) come from
the framework; add your own fields here and read them from ``app.settings``.

    {upper}_LOGGING__FORMAT=json   {upper}_EXECUTION__SYNC_WORKERS=16
    {upper}_DATABASE__URL=...{database_hint}
"""

from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="{upper}_", dotenv=".env"{toml}):
{fields}'''

SERVER_TOML = """# Which server `hinoki dev` and `hinoki serve` run: "kiso" (Hinoki's Rust
# server, the default) or "granian" (a general-purpose ASGI server).
# Override per environment with {upper}_SERVER__SERVER=kiso.
[server]
server = "granian"
"""

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

from hinoki import Actions, Schemas, Transactions  # add Models with your first model
from hinoki.rules import RuleSet

project_rules = RuleSet(
    # Anything not public on purpose names an access policy, and the policy names permissions.
    Actions.require_access(match="{name}.*", except_=["{name}.api.health"]),
    Actions.require_permissions(match="{name}.*", except_=["{name}.api.health"]),
    # Responses are concrete Schema types and lists are paginated.
    Actions.forbid_model_responses(match="{name}.*"),
    Actions.require_return_type(match="{name}.*"),
    Actions.require_pagination(match="{name}.*"),
    # Writes run in a transaction that rolls back before errors become HTTP.
    Transactions.require_atomic(),
    # Secrets never reach a client, and request text is bounded.
    Schemas.forbid_exposure("password_hash", "password", "token"),
    Schemas.require_string_limits(),
    # Turn these on with your first model (a model rule that matches no model
    # fails the check): tables are pinned and foreign keys are indexed.
    # Models.require_explicit_table(match="{name}.*"),
    # Models.require_foreign_key_index(match="{name}.*"),
)
'''

APP_API = '''"""Endpoints of the {name} app. Group actions that share a prefix, access and
transaction, then list the group in routes.py:

    items = RouteGroup("/items", access=staff, atomic=True)

    @items.get("/{{item_id}}")
    async def get_item(item_id: UUID) -> ItemOut: ...
"""
'''

APP_ROUTES = '''"""Reusable routes of the {name} app; mount them in `{package}/routes.py`."""

from hinoki import Includable

# from {package}.apps.{name}.api import items

routes: list[Includable] = []
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
      "args": ["run", "hinoki", "mcp"]
    }}
  }}
}}
"""

README = """# {title}

A Hinoki application.

```sh
uv sync                 # installs Hinoki and the development tools
uv run pytest           # the tests need no database
uv run hinoki dev                   # serve with reload on http://127.0.0.1:8000
uv run hinoki routes                # list the routes
uv run hinoki check                 # run the project's rules
uv run hinoki app create <name>     # add an app: its models, routes and migrations
uv run hinoki app create <name> --no-models  # add an API-only app without persistence
uv run hinoki migrations plan <app> # plan an app's migrations
uv run hinoki migrations apply      # initialize the local SQLite database
uv run hinoki manage                # list project management commands
```

Hinoki is pre-alpha and not published on PyPI: `pyproject.toml` names where `uv`
finds it under `[tool.uv.sources]`. Change that entry to pin a revision or to use
another checkout. `AGENTS.md` describes the conventions for people and agents.
In a project with `hinoki.toml`, migration commands root the implicit SQLite file
and a relative SQLite path from the settings beside that manifest, even when they
run from a subdirectory. Relative SQLite URLs given with `--database` or
`HINOKI_DATABASE_URL` use the current working directory, and so does the server:
start it from the project root.
"""

CLAUDE = "Read and follow `AGENTS.md` before changing anything in this repository.\n"

AGENTS = """# Working on {title} with an AI agent

A Hinoki application. Keep changes small, typed and checked.

## Framework documentation

Hinoki's documentation for agents is at https://hinoki.caspel.dev/llms.txt (an index that follows the current default docs version). The full text is at https://hinoki.caspel.dev/llms-full.txt and each page is plain Markdown at `<page>.md`. Prefer these and the development MCP over guessing from other frameworks: the FastAPI, Pydantic and Django APIs do not exist in Hinoki.

## Commands

```sh
uv sync
uv run pytest                                   # tests
uv run ruff check . && uv run ruff format .     # lint and format
uv run mypy {name}                              # types
uv run hinoki check                             # the project's rules
uv run hinoki dev                               # reload; `serve` runs it for production
```

`check` and the MCP server build the app without opening a database. A `database: settings.DatabaseSettings` field needs no value: it defaults to the local SQLite file `.hinoki/local.sqlite3`. Set `{upper}_DATABASE__URL` (and under `env` in `.mcp.json`) only to use another database. Tests that touch the database need a real disposable one in `HINOKI_TEST_DATABASE_URL`.

`check` runs the project's rules and prints diagnostics with stable IDs
(`HNK-ACC-001` access, `HNK-TXN-001` transactions, `HNK-SCH-001` exposed fields,
`HNK-IMP-001` import boundaries). A violation is a failure to fix, not to hide.

## Conventions

* Path, query and body values are typed parameters. The request object is optional: declare `request: Request` as the first parameter only when you need headers, cookies or the raw body.
* Public by default: any action that is not public on purpose names an `Access` policy (`access=editor`).
* Writes use `atomic=True`. Failures are exceptions, not `Response` values (a `Response` commits). Service code outside an action that must be atomic uses `async with db.atomic():` or `@db.atomic` (`from hinoki import db`).
* Request and response bodies are `Schema` classes. Never return a `Model`; list the fields a response may contain. Fields named in the exposure rule (`password_hash`, ...) never appear in a response schema.
* Relations are loaded explicitly (`select_related` / `prefetch_related`); there is no lazy loading.
* Create from a schema with `Model.acreate(data, owner_id=me)`: keyword values set by the server win over the client's. Build responses with `Out.from_model(row)`.
* Schema changes go through `hinoki migrations plan`, reviewed as SQL. Never apply them at startup.
* Configuration is typed settings (`{name}/settings.py`, loaded from `{upper}_*` environment variables); add fields there instead of reading `os.environ` in code. Secrets are `settings.Secret` fields.
* No underscores in file names; tests are `*test.py`.

## Adding a database and a protected resource

The generated project has no database or protected resource, and has an app only if `hinoki new` created one. Models live in apps, never in the project package: if `hinoki.toml` registers no app, run `uv run hinoki app create <name>` first, then work inside `{name}/apps/<name>/`. The reference is `examples/tracker` in the framework repository; the shape is:

1. `apps/<name>/models.py`: a `Member` (id, name, token) and your model with `owner_id: UUID = foreign_key(Member, on_delete=OnDelete.RESTRICT, related_name="...")` (`OnDelete` comes from `hinoki.orm`). Then turn on the two model rules in `rules.py` (`Models.require_explicit_table`, `Models.require_foreign_key_index`): pin each table with `table=` or `@db.table(...)`, and index each foreign key with `@db.indexed("owner_id")`.
2. `apps/<name>/schemas.py`: an `In` schema without the owner, an `Out` schema.
3. `apps/<name>/operations.py`: plain async functions taking `owner_id` and values; row permission goes in the statement (`Model.filter(owner_id=me)`, `aupdate_where({{...}})`), so someone else's row looks missing (404).
4. `apps/<name>/security.py`: a `BearerAuth` that resolves a token to `Caller(id=..., user=member, permissions=...)` and an `Access(...)`.
5. `apps/<name>/api.py`: actions with `access=...`, `caller: Caller[Member]`, `atomic=True` on writes, `pagination=` on lists; list them in `apps/<name>/routes.py`, and mount that list explicitly in `{name}/routes.py` with `Mount("/<name>", ...)`. `main.py` stays `Hinoki(settings=ProjectSettings, ...)`, which automatically loads `{name}/routes.py` from `hinoki.toml` and opens the database declared by the settings' `database` field (a URL, or `engine` with its connection fields).
6. `hinoki migrations plan <name>`, then review the SQL. The project manifest discovers app models and migration packages.
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


REPOSITORY = "https://github.com/caspel26/hinoki"


def git_source(spec: str) -> str:
    """``[tool.uv.sources]`` for a git URL, optionally ``URL@revision``."""
    url = spec.removeprefix("git+")
    head, separator, tail = url.rpartition("@")
    revision = ""
    if separator and tail and "/" not in tail and ":" not in tail:
        url, revision = head, tail
    extra = f", rev = {jsontext.toml_value(revision)}" if revision else ""
    return f"\n[tool.uv.sources]\nhinoki-api = {{ git = {jsontext.toml_value(url)}{extra} }}\n"


def framework_source(source: str | None = None) -> str:
    """Where ``uv sync`` finds ``hinoki-api``. It is not on PyPI yet, so the
    project always names a source: the ``--hinoki`` value (a checkout path or a
    git URL), else this checkout when Hinoki runs from a source tree, else the
    public repository."""
    if source is not None:
        if "://" in source or source.startswith("git+"):
            return git_source(source)
        root = Path(source).expanduser().resolve()
        if not (root / "pyproject.toml").is_file() or not (root / "crates").is_dir():
            raise ScaffoldError(f"{root} is not a Hinoki source checkout")
        return f"\n[tool.uv.sources]\nhinoki-api = {{ path = {jsontext.toml_value(str(root))}, editable = true }}\n"
    import hinoki  # noqa: PLC0415 - only needed to find the checkout

    root = Path(hinoki.__file__).resolve().parents[2]
    if (root / "crates").is_dir() and (root / "pyproject.toml").is_file():
        return f"\n[tool.uv.sources]\nhinoki-api = {{ path = {jsontext.toml_value(str(root))}, editable = true }}\n"
    return git_source(REPOSITORY)


class ScaffoldError(ValueError):
    """The project cannot be created as asked."""


DATABASES = ("none", "sqlite", "postgresql")
SERVERS = ("kiso", "granian")
AGENT_FILES = ("AGENTS.md", "CLAUDE.md", ".mcp.json")


def settings_source(name: str, database: str, server: str) -> str:
    """``settings.py``: the declared database and, for Granian, the TOML file."""
    return SETTINGS.format(
        name=name,
        upper=name.upper(),
        toml=', toml="settings.toml"' if server == "granian" else "",
        database_hint=(
            ""
            if database != "none"
            else "      (after you add: database: settings.DatabaseSettings)"
        ),
        fields=(
            "    database: settings.DatabaseSettings\n"
            if database != "none"
            else "    pass\n"
        ),
    )


def database_files(name: str, database: str, server: str) -> dict[str, str]:
    """Settings, TOML and environment example for the chosen database and server."""
    result = {f"{name}/settings.py": settings_source(name, database, server)}
    if server == "granian":
        result["settings.toml"] = SERVER_TOML.format(upper=name.upper())
    if database == "postgresql":
        result[".env.example"] = (
            f"{name.upper()}_DATABASE__URL=postgresql://user:password@localhost:5432/{name}\n"
        )
    return result


def files(
    name: str,
    source: str | None = None,
    *,
    database: str = "none",
    server: str = "kiso",
    agent_files: bool = True,
    skills: bool = False,
) -> dict[str, str]:
    """Relative path to content for a new project called ``name``."""
    if database not in DATABASES:
        raise ScaffoldError(f"the database must be one of {', '.join(DATABASES)}")
    if server not in SERVERS:
        raise ScaffoldError(f"the server must be one of {', '.join(SERVERS)}")
    title = name.capitalize()
    values = {
        "name": name,
        "title": title,
        "upper": name.upper(),
        "sources": framework_source(source),
    }
    contents = {
        "pyproject.toml": PYPROJECT.format(**values),
        "hinoki.toml": (
            "[project]\n"
            f'application = "{name}.main:app"\n'
            f'routes = "{name}.routes:routes"\n'
            f'rules = ["{name}.rules:project_rules"]\n'
        ),
        "README.md": README.format(**values),
        "AGENTS.md": AGENTS.format(**values),
        "CLAUDE.md": CLAUDE,
        ".mcp.json": MCP.format(**values),
        "hinoki-rules.toml": WAIVERS.format(**values),
        f"{name}/__init__.py": "",
        f"{name}/main.py": MAIN.format(**values),
        f"{name}/api.py": API.format(**values),
        f"{name}/routes.py": ROUTES.format(**values),
        f"{name}/rules.py": RULES.format(**values),
        "tests/__init__.py": "",
        "tests/healthtest.py": TEST.format(**values),
        ".gitignore": (
            ".venv/\n__pycache__/\n*.pyc\n.mypy_cache/\n.ruff_cache/\n.hinoki/\n"
            ".hinoki-app-create.lock\n.env\n"
        ),
    }
    contents.update(database_files(name, database, server))
    if not agent_files:
        for relative in AGENT_FILES:
            contents.pop(relative)
    if skills:
        contents.update(skill_files(name))
    return contents


def create(
    name: str,
    parent: Path,
    source: str | None = None,
    *,
    database: str = "none",
    server: str = "kiso",
    agent_files: bool = True,
    skills: bool = False,
) -> Path:
    """Write the project into ``parent / name``; refuse a bad name or a target
    that exists and is not empty."""
    if not NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise ScaffoldError(
            "the name must be lower-case letters and digits, with single underscores "
            "between words, starting with a letter (no dashes: it is also the package "
            "name), and must not shadow Hinoki, a Python keyword or a "
            "standard-library module"
        )
    target = parent / name
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"{target} already exists and is not empty")
    contents = files(
        name,
        source,
        database=database,
        server=server,
        agent_files=agent_files,
        skills=skills,
    )  # resolve the source before writing anything
    for relative, content in contents.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return target
