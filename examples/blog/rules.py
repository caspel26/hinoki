"""The blog's rules (run with ``hinoki check``, see the README).

Each line is an architectural promise the tool checks, not a style preference.
"""

from hinoki import Actions, Imports, Models, Schemas, Transactions
from hinoki.rules import RuleSet

PUBLIC = [
    "blog.api.health",
    "blog.api.Posts.listing",
    "blog.api.Posts.one",
    "blog.accounts.*.login",  # public on purpose: it is how you sign in
]

# Any signed-in user may read their own profile: no permission, on purpose.
SIGNED_IN = ["blog.accounts.*.me"]

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
    # Login only reads; holding a transaction open during a password hash would
    # tie up a connection for nothing.
    Transactions.require_atomic(except_=["blog.accounts.*.login"]),
    # These columns never reach a client, even through nested schemas.
    Schemas.forbid_exposure("token", "password_hash"),
    # Request text is bounded, so no client can send an unbounded string.
    Schemas.require_string_limits(),
    # Tables are pinned and foreign keys are indexed.
    Models.require_explicit_table(match="blog.*"),
    Models.require_foreign_key_index(match="blog.*"),
    # The persistence layer does not know about the HTTP layer.
    Imports.forbid(source="blog.models", target="blog.api"),
)
