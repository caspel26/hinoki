"""The blog's rules (run with ``python -m hinoki check``, see the README).

Each line is an architectural promise the tool checks, not a style preference.
"""

from hinoki import Actions, Imports, Schemas, Transactions
from hinoki.rules import RuleSet

PUBLIC = [
    "examples.blog.api.health",
    "examples.blog.api.Posts.listing",
    "examples.blog.api.Posts.one",
    "examples.blog.accounts.*.login",  # public on purpose: it is how you sign in
]

blog_rules = RuleSet(
    # Anything not public on purpose names an access policy.
    Actions.require_access(match="examples.blog.*", except_=PUBLIC),
    # Writes run in a transaction that rolls back before errors become HTTP.
    # Login only reads; holding a transaction open during a password hash would
    # tie up a connection for nothing.
    Transactions.require_atomic(except_=["examples.blog.accounts.*.login"]),
    # These columns never reach a client, even through nested schemas.
    Schemas.forbid_exposure("token", "password_hash"),
    # The persistence layer does not know about the HTTP layer.
    Imports.forbid(source="examples.blog.models", target="examples.blog.api"),
)
