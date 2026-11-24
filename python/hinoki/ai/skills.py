"""Optional agent skills for a generated project (``hinoki new --skills``).

Each skill is a short Markdown playbook that points an assistant at the real
Hinoki tools (the development MCP and the CLI) instead of restating them.
"""

SKILLS: dict[str, tuple[str, str]] = {
    "hinoki-add-endpoint": (
        "Add or change an HTTP action in this Hinoki project, with its Schemas, "
        "access policy and test.",
        """# Add an endpoint

1. Call the `inspect_project` tool to see the registered actions and the app's
   conventions. Call the `explain_action` tool on a similar action and follow it.
2. Declare typed `Schema` classes for the request and the response. Never return
   a `Model` directly.
3. Add the action in `{name}/apps/<app>/api.py` with `@http.get`, `@http.post`
   and so on (create the app first with `uv run hinoki app create <app>` if
   `hinoki.toml` registers none). Name an `Access` policy unless the action is
   public on purpose. Writes use `atomic=True`.
4. List the route in `{name}/apps/<app>/routes.py`, which `hinoki.toml` points
   to: an unlisted action is not served.
5. Write a test with `hinoki.testing.ASGIClient` first, then make it pass.
6. Run `uv run hinoki check`, which reads the application and the rules from
   `hinoki.toml`, and fix every diagnostic.
""",
    ),
    "hinoki-fix-rule": (
        "Fix a failing Hinoki rule diagnostic without weakening the rule.",
        """# Fix a rule violation

1. Run `uv run hinoki check --format json`, which reads the application and the
   rules from `hinoki.toml`, and read each diagnostic.
2. Call the `explain_error` tool with the rule id (for example `HNK-ACC-001`
   for access or `HNK-TXN-001` for transactions) and apply the fix it describes.
3. Call the `list_rules` tool to confirm which rules and waivers are active.
4. Change the code, never `hinoki-rules.toml` or a rule's `except_`: waivers are
   the owner's decision. If a rule looks wrong, report it and stop.
5. Re-run the check until it is clean, then run the tests.
""",
    ),
    "hinoki-review-api": (
        "Check that an API change does not break existing clients.",
        """# Review an API change

1. Save the OpenAPI document of the released version:
   `uv run hinoki openapi {name}.main:app --output openapi-baseline.json`.
2. After your change, run
   `uv run hinoki openapi {name}.main:app --breaking-against openapi-baseline.json`.
   Exit code 1 lists each breaking change (removed operations or response
   fields, new required request fields, narrowed enums, changed types).
3. Prefer an additive change. If a break is intended, say so in your report; do
   not edit the baseline to hide it.
4. Run `uv run hinoki routes {name}.main:app` to confirm the served routes.
""",
    ),
    "hinoki-test-api": (
        "Exercise every operation with generated valid and invalid request bodies.",
        """# Test an API with generated payloads

1. Build the cases from the application's own OpenAPI document:

   ```python
   from hinoki.ai.samples import request_cases
   from {name}.main import app

   cases = request_cases(app.openapi())
   ```

2. Each `RequestCase` has a `method`, a `path`, a JSON `body` and `invalid`.
   Send them with `hinoki.testing.ASGIClient`: valid bodies must be accepted,
   invalid ones must answer 422.
3. Paths with parameters need real values first; `CannotSample` marks schemas
   (such as a regular-expression pattern) that cannot be generated.
4. Use disposable databases and synthetic data only.
""",
    ),
}

PATH = ".claude/skills/{skill}/SKILL.md"


def skill_files(name: str) -> dict[str, str]:
    """Relative path to content for every skill of project ``name``."""
    return {
        PATH.format(skill=skill): (
            f"---\nname: {skill}\ndescription: {description}\n---\n\n"
            + body.replace("{name}", name)
        )
        for skill, (description, body) in SKILLS.items()
    }
