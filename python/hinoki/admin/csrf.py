"""CSRF protection for the admin's forms (double-submit token).

Each page carries a random token in a hidden field and in a ``hinoki_csrf``
cookie (``SameSite=Strict``, ``HttpOnly``). A POST is accepted only when both
are present, well formed and equal (constant-time compare). Authentication is
separate and stays the application's ``Access`` policy.
"""

from __future__ import annotations

import hmac
import re
import secrets

from hinoki.reply import set_cookie
from hinoki.request import Request

COOKIE = "hinoki_csrf"
FIELD = "csrf"
TOKEN = re.compile(r"[0-9a-f]{32}")


def issue(request: Request) -> tuple[str, bool]:
    """``(token, is_new)``: reuse the caller's valid cookie, else mint one."""
    existing = request.COOKIES.get(COOKIE, "")
    if TOKEN.fullmatch(existing):
        return existing, False
    return secrets.token_hex(16), True


def cookie_header(request: Request, token: str, prefix: str) -> tuple[str, str]:
    return set_cookie(
        COOKIE,
        token,
        path=prefix or "/",
        http_only=True,
        secure=request.scheme == "https",
        same_site="Strict",
    )


def valid(request: Request) -> bool:
    """The submitted field equals the cookie, and both look like our tokens."""
    cookie = request.COOKIES.get(COOKIE, "")
    sent = request.POST.get(FIELD, "")
    return bool(
        TOKEN.fullmatch(cookie)
        and TOKEN.fullmatch(sent)
        and hmac.compare_digest(cookie, sent)
    )
