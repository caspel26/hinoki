"""Authentication providers used by tests."""

from hinoki import BearerAuth, Principal

EDITOR = Principal(
    subject="u-editor", identity="editor", scopes=frozenset({"posts:write"})
)
READER = Principal(subject="u-reader", identity="reader", scopes=frozenset())
TOKENS = {"editor-token": EDITOR, "reader-token": READER}


class TokenAuth(BearerAuth[str]):
    """Looks tokens up in a fixed table."""

    async def resolve_token(self, token: str) -> Principal[str] | None:
        return TOKENS.get(token)
