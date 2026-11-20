"""Session authentication for the admin's own users."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hinoki import core
from hinoki.auth import BaseAuth, Caller, Credentials
from hinoki.errors import APIError
from hinoki.http.response import set_cookie

if TYPE_CHECKING:
    from hinoki.admin.core import Admin
    from hinoki.admin.users.models import AdminUser

COOKIE = "hinoki_admin_session"
BASE_PERMISSION = "admin"  # every signed-in admin user holds it
EVERYTHING = "*"  # a superuser holds it


def permissions_of(
    user: AdminUser, grants: frozenset[tuple[str, str]]
) -> frozenset[str]:
    """What the user may do, as ``Caller`` permissions: ``admin``, then either ``*``
    (a superuser) or one ``slug:action`` string for each grant."""
    if user.is_superuser:
        return frozenset({BASE_PERMISSION, EVERYTHING})
    return frozenset(
        {BASE_PERMISSION, *(f"{slug}:{action}" for slug, action in grants)}
    )


class AdminSessionAuth(BaseAuth["AdminUser"]):
    """Resolves the signed-in admin user from the session cookie.

    Without a live session the answer is a ``303`` to the sign-in page, because
    the admin is used from a browser; the response says nothing else."""

    def __init__(self, admin: Admin) -> None:
        self.admin = admin

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "cookie", "name": self.admin.session_cookie}

    async def authenticate(self, credentials: Credentials) -> Caller[AdminUser] | None:
        accounts = self.admin.accounts
        cookies = core.parse_cookie_header(credentials.headers.get("cookie", ""))
        user = await accounts.user_for(cookies.get(self.admin.session_cookie, ""))
        if user is None:
            raise APIError(
                "Sign in to continue.",
                status=303,
                code="login_required",
                headers={"location": f"{self.admin.base}/login"},
            )
        grants = frozenset() if user.is_superuser else await accounts.grants(user.id)
        return Caller(
            id=user.username, user=user, permissions=permissions_of(user, grants)
        )


def session_cookie(
    token: str, base: str, *, hours: int, secure: bool, name: str = COOKIE
) -> tuple[str, str]:
    """The ``set-cookie`` header that holds a session: HttpOnly, scoped to the
    admin's path and lasting no longer than the session."""
    return set_cookie(
        name,
        token,
        path=base or "/",
        max_age=hours * 3600,
        http_only=True,
        secure=secure,
        same_site="Lax",
    )


def end_cookie(base: str, *, secure: bool, name: str = COOKIE) -> tuple[str, str]:
    """The ``set-cookie`` header that removes the session cookie."""
    return set_cookie(
        name,
        "x",
        path=base or "/",
        max_age=0,
        http_only=True,
        secure=secure,
        same_site="Lax",
    )
