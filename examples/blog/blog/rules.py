"""The blog's rules (run with ``hinoki check``, see the README).

Each line is an architectural promise the tool checks, not a style preference.
"""

from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

PUBLIC = [
    "blog.apps.blog.api.health",
    "blog.apps.blog.api.Posts.listing",
    "blog.apps.blog.api.Posts.one",
    "blog.apps.blog.accounts.*.login",  # public on purpose: it is how you sign in
]

# Any signed-in user may read their own profile: no permission, on purpose.
SIGNED_IN = ["blog.apps.blog.accounts.*.me"]

blog_rules = RuleSet(
    # Anything not public on purpose names an access policy.
    Actions.require_access(match="blog.*", except_=PUBLIC),
    # "Any signed-in caller" is never implicit: every policy names a permission.
    Actions.require_permissions(match="blog.*", except_=[*PUBLIC, *SIGNED_IN]),
    # Responses are concrete Schema types, and lists are always paginated.
    Actions.forbid_model_responses(match="blog.*"),
    Actions.require_return_type(match="blog.*"),
    Actions.require_pagination(match="blog.*"),
    # Writes run in a transaction that rolls back before errors become HTTP.
    # The MCP endpoint only dispatches to actions that have their own.
    Transactions.require_atomic(except_=["hinoki.ai.mcpapp.*"]),
    # These columns never reach a client, even through nested schemas.
    Schemas.forbid_exposure("token_hash", "password_hash"),
    # Request text is bounded, so no client can send an unbounded string.
    Schemas.require_string_limits(),
    # Tables are pinned and foreign keys are indexed.
    Models.require_explicit_table(match="blog.*"),
    Models.require_foreign_key_index(match="blog.*"),
    # The persistence layer does not know about the HTTP layer.
    Imports.forbid(source="blog.apps.blog.models", target="blog.apps.blog.api"),
)
