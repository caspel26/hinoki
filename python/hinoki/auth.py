"""Authentication providers, principals and policy enforcement.

Endpoints are public unless an action names an ``Access`` policy. A provider
turns request credentials into a ``Principal``; scopes are application
permissions checked after authentication (401 -> no/invalid credentials,
403 -> valid identity without the required scope).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass

from hinoki.blocking import call_hook
from hinoki.httperrors import Unauthorized


@dataclass(frozen=True, slots=True, init=False)
class Caller[T]:
    """Who is making the request, as decided by authentication.

    ``id`` is a stable string, ``user`` is your own object (the user row, a
    service account) and ``permissions`` is what the caller may do::

        Caller(id="u1", user=user, permissions=frozenset({"posts:write"}))

    The older names ``Principal``, ``subject``, ``identity`` and ``scopes`` are
    accepted everywhere as aliases.
    """

    id: str
    user: T
    permissions: frozenset[str]

    def __init__(
        self,
        id: str | None = None,
        user: T | None = None,
        permissions: Iterable[str] | None = None,
        *,
        subject: str | None = None,
        identity: T | None = None,
        scopes: Iterable[str] | None = None,
    ) -> None:
        chosen = id if id is not None else subject
        if chosen is None:
            raise TypeError("Caller needs an id")
        held = permissions if permissions is not None else (scopes or ())
        object.__setattr__(self, "id", chosen)
        object.__setattr__(self, "user", user if user is not None else identity)
        object.__setattr__(self, "permissions", frozenset(held))

    @property
    def subject(self) -> str:
        """Older name of ``id``."""
        return self.id

    @property
    def identity(self) -> T:
        """Older name of ``user``."""
        return self.user

    @property
    def scopes(self) -> frozenset[str]:
        """Older name of ``permissions``."""
        return self.permissions


Principal = Caller


@dataclass(frozen=True, slots=True)
class Credentials:
    """The parts of a request a provider may inspect."""

    headers: dict[str, str]
    query: dict[str, list[str]]


class BaseAuth[T](ABC):
    """Base class of providers.

    ``authenticate`` returns a principal, ``None`` when the request carries no
    credentials, and raises ``Unauthorized`` when credentials are present but
    invalid. Providers hold no per-request state.
    """

    challenge: str | None = None

    def openapi_scheme(self) -> dict[str, object] | None:
        """OpenAPI security scheme describing this provider, if it has one."""
        return None

    @abstractmethod
    def authenticate(
        self, credentials: Credentials
    ) -> Principal[T] | None | Awaitable[Principal[T] | None]:
        """``async def`` or plain ``def`` (a plain one runs on the sync executor)."""


class HeaderAuth[T](BaseAuth[T]):
    """Credential carried in a named header; subclasses implement ``resolve``."""

    header = "authorization"

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "header", "name": self.header}

    @abstractmethod
    def resolve(
        self, value: str
    ) -> Principal[T] | None | Awaitable[Principal[T] | None]:
        """The principal for a header value or ``None`` if invalid; ``async def``
        or plain ``def``."""

    async def authenticate(self, credentials: Credentials) -> Principal[T] | None:
        value = credentials.headers.get(self.header.lower())
        if value is None:
            return None
        principal: Principal[T] | None = await call_hook(self.resolve, value)
        if principal is None:
            raise Unauthorized("Invalid credentials.", headers=self.headers())
        return principal

    def headers(self) -> dict[str, str]:
        return {"www-authenticate": self.challenge} if self.challenge else {}


class APIKeyAuth[T](HeaderAuth[T]):
    """API key in ``x-api-key`` (override ``header`` to change it)."""

    header = "x-api-key"
    challenge = "ApiKey"


class BearerAuth[T](HeaderAuth[T]):
    """``Authorization: Bearer <token>``; subclasses implement ``resolve_token``."""

    challenge = "Bearer"

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "http", "scheme": "bearer"}

    @abstractmethod
    def resolve_token(
        self, token: str
    ) -> Principal[T] | None | Awaitable[Principal[T] | None]:
        """``async def`` or plain ``def``, as ``resolve``."""

    async def resolve(self, value: str) -> Principal[T] | None:
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        found: Principal[T] | None = await call_hook(self.resolve_token, token.strip())
        return found
