<p align="center">
  <img src="assets/hinoki-banner.png" alt="Hinoki — Python API framework" width="720">
</p>

<p align="center">
  <b>Typed Python APIs with a native Rust core.</b><br>
  Plain Python on the surface, verifiable architecture underneath.
</p>

<p align="center">
  <a href="https://github.com/caspel26/hinoki/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/caspel26/hinoki/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-b9a1f5">
  <img alt="Rust core" src="https://img.shields.io/badge/core-Rust%20%2B%20PyO3-b9a1f5">
  <img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-orange">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

---

> [!WARNING]
> **Hinoki is pre-alpha and not usable yet.** The design is settled and the first
> pieces of the core exist (see [What works today](#what-works-today)), but there is
> no HTTP server integration, ORM, auth or docs generator yet. Nothing is published
> to PyPI. Do not build on it.

Hinoki is an independent framework for REST APIs and modular services. You write
ordinary, fully typed Python — functions or classes, sync or async — and Hinoki turns
those declarations into one registry that routing, validation, policies, OpenAPI and
development tooling all share. It is **not** built on Django, FastAPI, Starlette or
Pydantic.

## The shape of the API

This is the confirmed target syntax ([`docs/`](docs) tracks what is implemented):

```python
from uuid import UUID

from hinoki import API, Access, HinokiAPI, get, post
from hinoki.auth import Principal

editor = Access(auth=blog_auth, scopes={Scope.POSTS_WRITE})


@get("/health")
def health() -> HealthOut:
    return HealthOut(status="ok")


class Posts(API, path="/posts"):
    @get("/{post_id}")
    async def retrieve(self, post_id: UUID) -> PostOut:
        return PostOut.from_model(await Post.aget(id=post_id))

    @post("/", status=201, access=editor, transaction=True)
    async def create(self, data: PostCreate, principal: Principal[User]) -> PostOut:
        ...


def create_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(health, Posts)
    return app
```

* **Plain Python.** `@get`/`@post`/`@put`/`@patch`/`@delete`, normal functions and classes,
  composable with your own decorators. No second grammar, no routing in docstrings.
* **Honest types.** `-> PostOut` means the function returns a `PostOut`. Signatures survive
  decoration, so your IDE and type checker keep working.
* **Public by default, protected on purpose.** Policies are reusable `Access` objects named
  on the operation; declaring a provider protects nothing by itself.
* **Explicit transactions.** `transaction=True` on the action: rollback happens before
  the error becomes an HTTP response.
* **Declared, not discovered.** Importing a module registers no routes and opens no
  connection. Resources belong to modules with a lifespan.

## What makes it different (the goals)

| Goal | What it means |
| --- | --- |
| **Optional, deterministic rules** | Declare architectural boundaries; violations produce diagnostics with a stable ID, location and phase. Off unless you enable them. |
| **Real context for AI tooling** | A development MCP server exposes the live registry, schemas and rule results — not a guess from reading files. |
| **Consistent customization** | Hand-written and generated (CRUD) operations share the same auth, errors and docs. Replace one operation without forking. |
| **Explicit modules** | Dependencies, resources and lifespan are inspectable; a module can grow into its own service. |
| **Native core** | Routing, request handling and the ORM engine in Rust; the Python/Rust boundary and its costs are documented, not hidden. |

Performance claims will appear only with reproducible benchmarks. There are none yet.

## What works today

Honest status, verified by the test suite:

| Area | State |
| --- | --- |
| HTTP method decorators, `Access`, `API` classes, `HinokiAPI.include` | Implemented; typed and tested |
| Native router (`hinoki.native.Router`): static + `{param}` paths, literal-over-param priority, 404/405, duplicate and conflict detection | Implemented in Rust, with Rust and Python tests |
| Static typing contract for the above (positive and negative fixtures) | Implemented, checked with `mypy --strict` |
| Granian lifespan behaviour (failed startup, per-worker lifespan, SIGTERM drain) | Characterised with real-server tests |
| Runtime design: Python/Rust/Granian boundary, resource ownership, sync executor, shutdown | [Designed](docs/RUNTIME-DESIGN.md), not implemented |
| ASGI adapter, schemas, ORM, auth, errors, pagination, OpenAPI | Not started |
| CRUD generation, rules engine, MCP, admin extra | Not started |

```python
>>> from hinoki import HinokiAPI, get
>>> @get("/users/{user_id}")
... def user(user_id: int) -> dict[str, int]:
...     return {"id": user_id}
>>> app = HinokiAPI(); app.include(user)
>>> route, params = app.resolve("GET", "/users/42")
>>> route.name, params
('user', {'user_id': '42'})
```

## Architecture

```
 client ─▶ Granian (Rust: hyper/tokio) ─▶ hinoki ASGI adapter (Python, thin)
                                              │
                                              ▼
                              hinoki.native (Rust, PyO3)
                              router · limits · (next) request decoding
                                              │
                                              ▼
                  registry ─▶ pipeline: auth → transaction → handler → output check
                                              │
                                              ▼
                              ORM engine (Rust): query AST → SQL → pool
```

The ASGI path crosses Python objects and is documented as such. Read
[`docs/RUNTIME-DESIGN.md`](docs/RUNTIME-DESIGN.md) for the exact boundary, resource
ownership per worker, the bounded sync executor and the lifespan/shutdown sequence, and
[`docs/adr/`](docs/adr) for the decisions and rejected alternatives.

## Roadmap

1. **Runtime** — lifespan coordinator, ASGI adapter, bounded sync executor *(designed)*
2. **Vertical slice: blog** — request → schema → ORM (PostgreSQL) → auth → rollback → OpenAPI → pagination
3. **ORM and generated CRUD** — relations, eager loading, replaceable operations
4. **Rules and development MCP**
5. **Admin extra** — one theme, full HTML override
6. **Stabilization** — wheels, benchmarks, release policy

The working queue lives in [`handoff.md`](handoff.md).

## Development

```sh
uv sync --locked                      # venv + builds the Rust extension via maturin
uv run --locked pytest --cov          # Python tests
uv run --locked mypy                  # strict types + typing fixtures
uv run --locked ruff check . && uv run --locked ruff format --check .
cargo test --workspace                # Rust tests
```

Requirements: [uv](https://docs.astral.sh/uv/), a stable Rust toolchain, Python 3.12+.
Full gate list: [`docs/TOOLCHAIN.md`](docs/TOOLCHAIN.md) · Contributing:
[`CONTRIBUTING.md`](CONTRIBUTING.md) · Agent rules: [`AGENTS.md`](AGENTS.md).

## Brand

The approved identity — a folded ribbon with a single sprout and lilac dots — is in
[`assets/`](assets). Palette: deep aubergine `#1a0f22`, lilac `#cdaefd`, off-white `#fbfbfb`.

## License

[MIT](LICENSE) © 2026 Caspel26
