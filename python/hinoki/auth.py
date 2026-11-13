"""Authentication providers, principals and policy enforcement.

Endpoints are public unless an action names an ``Access`` policy. A provider
turns request credentials into a ``Principal``; scopes are application
permissions checked after authentication (401 -> no/invalid credentials,
403 -> valid identity without the required scope).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from hinoki.httperrors import Unauthorized


@dataclass(frozen=True, slots=True)
class Principal[T]:
    """Who is calling: the token subject, the application identity and scopes."""

    subject: str
    identity: T
    scopes: frozenset[str] = field(default_factory=frozenset)


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
    async def authenticate(self, credentials: Credentials) -> Principal[T] | None: ...


class HeaderAuth[T](BaseAuth[T]):
    """Credential carried in a named header; subclasses implement ``resolve``."""

    header = "authorization"

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "header", "name": self.header}

    @abstractmethod
    async def resolve(self, value: str) -> Principal[T] | None:
        """Return the principal for a header value or ``None`` if invalid."""

    async def authenticate(self, credentials: Credentials) -> Principal[T] | None:
        value = credentials.headers.get(self.header.lower())
        if value is None:
            return None
        principal = await self.resolve(value)
        if principal is None:
            raise Unauthorized("Invalid credentials.", headers=self.headers())
        return principal

    def headers(self) -> dict[str, str]:
        return {"www-authenticate": self.challenge} if self.challenge else {}


class APIKeyAuth[T](HeaderAuth[T]):
    """API key in ``x-api-key`` (override ``header`` to change it)."""

    header = "x-api-key"


class BearerAuth[T](HeaderAuth[T]):
    """``Authorization: Bearer <token>``; subclasses implement ``resolve_token``."""

    challenge = "Bearer"

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "http", "scheme": "bearer"}

    @abstractmethod
    async def resolve_token(self, token: str) -> Principal[T] | None: ...

    async def resolve(self, value: str) -> Principal[T] | None:
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return await self.resolve_token(token.strip())
