"""Accounts, sessions and grants of the admin's own users.

Passwords are Argon2id hashes. A session is a random token whose SHA-256 is stored,
so a stolen database cannot be replayed; the cookie holds the token itself. Every
function uses the database of the current context, like any model call, and nothing
runs at import.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from hinoki.admin.users.models import AdminGrant, AdminSession, AdminUser
from hinoki.crypto import PasswordHasher
from hinoki.orm.context import current_database
from hinoki.settings import AdminSettings
from hinoki.utils import utc_now

USERNAME = re.compile(r"[a-z0-9][a-z0-9_.@+-]{0,149}")
ACTIONS = ("view", "add", "change", "delete")
MAX_PASSWORD_BYTES = 1024  # a bound before hashing: Argon2 cost grows with input
TOUCH_SECONDS = 60  # last_seen is refreshed at most this often


class AdminUserError(ValueError):
    """A username or password that the admin refuses; the message is for the person."""


class AdminAccounts:
    """Create users, check passwords, and open, resolve and close sessions."""

    def __init__(
        self,
        settings: AdminSettings | None = None,
        *,
        hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings or AdminSettings()
        self.hasher = hasher or PasswordHasher()
        self.clock = clock
        self.decoy = self.hasher.hash_sync("not-the-password-of-anyone")

    # -- users ---------------------------------------------------------------

    def clean_username(self, username: str) -> str:
        name = username.strip().lower()
        if not USERNAME.fullmatch(name):
            raise AdminUserError(
                "A username is 1 to 150 letters, digits and . _ @ + - characters."
            )
        return name

    def check_password(self, password: str) -> None:
        least = self.settings.min_password_length
        if len(password) < least:
            raise AdminUserError(f"The password needs at least {least} characters.")
        if len(password.encode()) > MAX_PASSWORD_BYTES:
            raise AdminUserError("The password is too long.")

    async def create_user(
        self,
        username: str,
        password: str,
        *,
        superuser: bool = False,
        active: bool = True,
    ) -> AdminUser:
        name = self.clean_username(username)
        self.check_password(password)
        if await AdminUser.filter(username=name).acount():
            raise AdminUserError("That username is already taken.")
        return await AdminUser.acreate(
            username=name,
            password_hash=await self.hasher.hash(password),
            is_superuser=superuser,
            is_active=active,
            created_at=self.clock(),
        )

    async def set_password(self, user: AdminUser, password: str) -> None:
        """Change a password and end every session of the user."""
        self.check_password(password)
        hashed = await self.hasher.hash(password)  # no connection is held meanwhile
        async with current_database().transaction():
            user.password_hash = hashed
            await user.asave()
            await self.revoke_sessions(user)

    async def set_active(self, user: AdminUser, active: bool) -> None:
        """Activate or deactivate a user. Deactivating also ends every session, so
        reactivating never brings an old one back."""
        async with current_database().transaction():
            user.is_active = active
            await user.asave()
            if not active:
                await self.revoke_sessions(user)

    async def verify(self, username: str, password: str) -> AdminUser | None:
        """The active user when the password matches, else ``None``. The same work
        is done for an unknown name, so timing does not say who exists."""
        name = username.strip().lower()
        user = None
        if len(password.encode()) <= MAX_PASSWORD_BYTES:
            user = await AdminUser.aget_or_none(username=name)
        stored = user.password_hash if user is not None else self.decoy
        genuine = await self.hasher.verify(password, stored)
        if user is None or not genuine or not user.is_active:
            return None
        return user

    # -- sessions --------------------------------------------------------------

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def fingerprint(user: AdminUser) -> str:
        return hashlib.sha256(user.password_hash.encode()).hexdigest()

    async def open_session(self, user: AdminUser, user_agent: str = "") -> str:
        """A new session for ``user``; the returned token is the cookie value. It is
        bound to the password the user has now, so a later password change ends it."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        async with current_database().transaction():
            await AdminSession.acreate(
                token_hash=self.digest(token),
                user_id=user.id,
                created_at=now,
                expires_at=now + timedelta(hours=self.settings.session_hours),
                last_seen_at=now,
                user_agent=user_agent[:200],
                fingerprint=self.fingerprint(user),
            )
            user.last_login = now
            await user.asave()
        return token

    async def user_for(self, token: str) -> AdminUser | None:
        """The active user a live session belongs to; an expired one is removed."""
        if not token or len(token) > MAX_PASSWORD_BYTES:
            return None
        session = await AdminSession.aget_or_none(token_hash=self.digest(token))
        if session is None or not await self.live(session):
            return None
        user = await AdminUser.aget_or_none(id=session.user_id)
        if user is None or not user.is_active:
            return None
        return user if session.fingerprint == self.fingerprint(user) else None

    async def live(self, session: AdminSession) -> bool:
        """Whether the session is within both of its lifetimes; an ended one is
        deleted and a live one remembers it was just used."""
        now = self.clock()
        idle = timedelta(minutes=self.settings.idle_minutes)
        if now >= session.expires_at or now - session.last_seen_at >= idle:
            await AdminSession.filter(id=session.id).adelete()
            return False
        if (now - session.last_seen_at).total_seconds() >= TOUCH_SECONDS:
            await AdminSession.aupdate(id=session.id, last_seen_at=now, returning=False)
        return True

    async def close_session(self, token: str) -> None:
        await AdminSession.filter(token_hash=self.digest(token)).adelete()

    async def revoke_sessions(
        self, user: AdminUser, *, keep: str | None = None
    ) -> None:
        """End the user's sessions, except the one holding ``keep``."""
        if keep:
            await AdminSession.filter(
                user_id=user.id, token_hash__ne=self.digest(keep)
            ).adelete()
        else:
            await AdminSession.filter(user_id=user.id).adelete()

    async def purge_expired(self) -> int:
        """Delete sessions past their absolute expiry; returns how many."""
        return await AdminSession.filter(expires_at__lte=self.clock()).adelete()

    # -- grants ------------------------------------------------------------------

    async def grants(self, user_id: UUID) -> frozenset[tuple[str, str]]:
        rows = await AdminGrant.filter(user_id=user_id)
        return frozenset((row.model, row.action) for row in rows)

    async def set_grants(
        self, user: AdminUser, grants: set[tuple[str, str]] | frozenset[tuple[str, str]]
    ) -> None:
        """Replace the user's grants with exactly ``grants`` (one transaction)."""
        for _model, action in grants:
            if action not in ACTIONS:
                raise AdminUserError(f"Unknown permission {action!r}.")
        async with current_database().transaction():
            await AdminGrant.filter(user_id=user.id).adelete()
            for model, action in sorted(grants):
                await AdminGrant.acreate(user_id=user.id, model=model, action=action)
