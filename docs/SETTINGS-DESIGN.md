# Built-in typed settings — design (Q017)

Status: **implemented first slice** (2026-11-13). Owner request: the framework has typed built-in settings (the spec: `FRAMEWORK_SPEC.md` "Settings", `RUNTIME_AND_MODULES.md`) and they were not visible.

## Observed before

Configuration was scattered: `RuntimeConfig` (workers, timeouts) passed as `runtime=`, a module constant for the body limit, titles and doc URLs as `HinokiAPI(...)` keywords, `DatabaseModule(url, max_size)`, `Admin(secret=...)`. `HinokiAPI(settings=...)` and `ctx.settings` existed but held whatever object you gave. Nothing read the environment, nothing validated a group, and nothing was shown to the operator.

## Decisions

* **Plain frozen dataclasses, no Pydantic.** A `Settings` subclass is a keyword-only frozen dataclass; fields are typed; groups nest (`settings.execution.sync_workers`).
* **Loading is explicit:** `Settings.load(prefix="BLOG_", dotenv=".env", overrides={...})`. Precedence, highest first: `overrides`, the process environment, the dotenv file, the field default. Nested names use a double underscore: `BLOG_EXECUTION__SYNC_WORKERS=16`.
* **Typo guard** (`strict=True`, the default; `strict=False` when another program shares the prefix; no check with an empty prefix): an environment variable that starts with the prefix but names no field is an error, listed with the others; every problem is reported at once, never a half-built object. A required field (no default) that is missing is an error.
* **Types:** `str`, `int`, `float`, `bool` (`true/false/1/0/yes/no/on/off`), `Secret`, `tuple[str, ...]` or `list[str]` (comma separated; prefer the tuple, which stays immutable), `X | None` (empty string is `None`), `Literal[...]`, nested `Settings`. No implicit coercion beyond that.
* **Secrets:** `Secret` wraps a string; `repr`/`str` print `***`; `reveal()` returns the value. Error messages never contain a value, only the key and the reason. `settings.describe()` returns the effective configuration with secrets redacted (what an operator or the dev MCP may show).
* **Built-in groups** (`FrameworkSettings`): `execution` (sync workers and queue, request timeout, body limit), `lifecycle` (startup and shutdown timeouts), `logging` (level, text or JSON), `docs` (title, version, OpenAPI/Swagger/ReDoc URLs). `DatabaseSettings` (`url: Secret`, `max_size`) is offered for your own settings class. Your settings subclass `FrameworkSettings` and add groups or fields; modules read theirs from `ctx.settings`.
* **Wiring:** `HinokiAPI(settings=settings)` derives the runtime limits, the docs URLs and the logging configuration from it; an explicit keyword argument (`title=`, `runtime=`) still wins. Logging is applied once per worker at startup, never at import.
* **Not included yet (stated, not faked):** a `server` group (host, port, workers, Granian options) because there is no `serve` command that applies it; settings from TOML files or secret managers; live reload.

## Verification

Unit tests for every type and error path (all problems reported together, secrets never echoed, precedence, dotenv parsing, unknown keys), the wiring into `HinokiAPI`, the body limit being enforced from settings, the JSON log format, and typing fixtures. An independent review follows because secrets and environment parsing are security-adjacent.

## Review (2026-11-13) and what changed

An independent review found nine points; fixed: explicit `docs_url=None` now disables a page the settings enable (a sentinel, not `None`, means "unset"); the typo guard has a `strict` switch; numbers are parsed strictly (ASCII digits, no underscores, at most 18 digits) and `max_body_bytes` / `sync_workers` have upper bounds; group overrides that are not mappings are an error; `Secret` fields built directly in code are wrapped; `Secret` equality is constant-time; tuple fields; dotenv inline comments; the log handler writes to the `sys.stderr` of the moment; a declared `Content-Length` above the limit is refused with 413 before the body is read; type hints are cached. Documented rather than changed: `validate()` messages are shown, so they must not contain values; settings pickled into a queue contain their secrets in plain text; the `hinoki` logger still propagates to the root logger; with several apps in one process the last `LoggingSettings.apply` wins; an explicit `runtime=` replaces the settings-derived runtime as a whole.
