"""The promises Mailroom keeps, run by ``hinoki check``.

Mail is sent on a caller's request, so every route names an access policy, request
strings are bounded, and every template matches its context.
"""

from hinoki import Actions, Schemas
from hinoki.rules import EmailTemplatesMatch, RuleSet

policy = RuleSet(
    Actions.require_access(match="mailroom.*"),
    Schemas.require_string_limits(),
    EmailTemplatesMatch(),
)
