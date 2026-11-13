"""CSRF protection for the admin's forms.

A token is ``<nonce>.<mac>`` where the MAC is HMAC-SHA256 over the signed-in
subject and the nonce, keyed by a server secret. It travels twice, in a hidden
form field and in a ``hinoki_csrf`` cookie (``SameSite=Strict``, ``HttpOnly``).
A POST is accepted only when the field equals the cookie, the MAC verifies for
the *current* subject, and the browser's ``Origin`` / ``Sec-Fetch-Site`` (when
sent) say the request came from this site. A token invented by an attacker, for
example through a cookie set from a sibling subdomain, fails the MAC.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

from hinoki.reply import set_cookie
from hinoki.request import Request

COOKIE = "hinoki_csrf"
FIELD = "csrf"
TOKEN = re.compile(r"[0-9a-f]{32}\.[0-9a-f]{32}")
MIN_SECRET = 32
CROSS_SITE = {"cross-site", "same-site"}


class Csrf:
    """Token issue and verification for one admin."""

    def __init__(self, secret: str | bytes) -> None:
        key = secret.encode() if isinstance(secret, str) else bytes(secret)
        if len(key) < MIN_SECRET:
            raise ValueError(f"the admin secret needs at least {MIN_SECRET} bytes")
        self.key = key

    def mac(self, subject: str, nonce: str) -> str:
        message = f"{subject}\x00{nonce}".encode()
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()[:32]

    def mint(self, subject: str) -> str:
        nonce = secrets.token_hex(16)
        return f"{nonce}.{self.mac(subject, nonce)}"

    def genuine(self, token: str, subject: str) -> bool:
        if not TOKEN.fullmatch(token):
            return False
        nonce, _, mac = token.partition(".")
        return hmac.compare_digest(mac, self.mac(subject, nonce))

    def issue(self, request: Request, subject: str) -> tuple[str, bool]:
        """``(token, is_new)``: reuse the caller's genuine cookie, else mint one."""
        existing = request.COOKIES.get(COOKIE, "")
        if self.genuine(existing, subject):
            return existing, False
        return self.mint(subject), True

    def valid(self, request: Request, subject: str) -> bool:
        """Cookie equals field, the MAC is ours for this subject, same site."""
        cookie = request.COOKIES.get(COOKIE, "")
        sent = request.POST.get(FIELD, "")
        return (
            same_site(request)
            and hmac.compare_digest(cookie.encode(), sent.encode())
            and self.genuine(cookie, subject)
        )


def same_site(request: Request) -> bool:
    """Refuse requests the browser says are cross-site; absent headers pass (the
    MAC check still applies)."""
    if request.headers.get("sec-fetch-site", "") in CROSS_SITE:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin != "null" and urlsplit(origin).netloc == request.host


def cookie_header(
    request: Request, token: str, prefix: str, *, secure: bool | None = None
) -> tuple[str, str]:
    """The ``set-cookie`` header; ``secure=None`` follows the request scheme (set
    it explicitly behind a TLS-terminating proxy)."""
    return set_cookie(
        COOKIE,
        token,
        path=prefix or "/",
        http_only=True,
        secure=request.scheme == "https" if secure is None else secure,
        same_site="Strict",
    )
