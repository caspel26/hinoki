"""Authentication providers used by tests."""

from hinoki import BearerAuth, Caller

EDITOR = Caller(id="u-editor", user="editor", permissions=frozenset({"posts:write"}))
READER = Caller(id="u-reader", user="reader", permissions=frozenset())
TOKENS = {"editor-token": EDITOR, "reader-token": READER}


class TokenAuth(BearerAuth[str]):
    """Looks tokens up in a fixed table."""

    async def resolve_token(self, token: str) -> Caller[str] | None:
        return TOKENS.get(token)
