"""Authentication providers, principals and policy enforcement.

Endpoints are public unless an action names an ``Access`` policy. A provider
turns request credentials into a ``Caller``; permissions are application
rules checked after authentication (401 -> no/invalid credentials,
403 -> valid caller without the required permission).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, cast

from hinoki import core
from hinoki.blocking import call_hook, is_coroutine_hook
from hinoki.errors import Unauthorized

_EMPTY_FROZENSET: frozenset[str] = frozenset()
_EMPTY_CLAIMS: Mapping[str, Any] = MappingProxyType({})


class Permission(StrEnum):
    """Base for an application's typed permission names.

    Subclass it with the permissions your application uses::

        class BlogPermission(Permission):
            WRITE = "posts:write"

    Members compare and serialize as their string values.
    """


@dataclass(frozen=True, slots=True, init=False)
class Caller[T]:
    """Who is making the request, as decided by authentication.

    ``id`` is a stable string, ``user`` is your own object (the user row, a
    service account) and ``permissions`` is what the caller may do::

        Caller(id="u1", user=user, permissions=frozenset({"posts:write"}))
    """

    id: str
    user: T
    permissions: frozenset[str]
    claims: Mapping[str, Any]

    def __init__(
        self,
        id: str,
        user: T,
        permissions: Iterable[str] = (),
        claims: Mapping[str, Any] = _EMPTY_CLAIMS,
    ) -> None:
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "user", user)
        if isinstance(permissions, str | bytes):
            raise TypeError("permissions is a list of names, not one string")
        resolved_permissions = (
            frozenset(str(p) for p in permissions) if permissions else _EMPTY_FROZENSET
        )
        # Plain strings, as ``Access`` stores them: a ``Permission`` member
        # equals its value, so checks match either way.
        object.__setattr__(self, "permissions", resolved_permissions)
        resolved_claims = MappingProxyType(dict(claims)) if claims else _EMPTY_CLAIMS
        object.__setattr__(self, "claims", resolved_claims)


class _LazyHeaders(Mapping[str, str]):
    """Read-only mapping backed by ``PreparedRequest`` that resolves single
    header lookups in Rust and materializes ``header_dict`` only when iterated
    or measured."""

    __slots__ = ("_dict", "_request")
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, request: core.PreparedRequest) -> None:
        self._request = request
        self._dict: dict[str, str] | None = None

    def _materialize(self) -> dict[str, str]:
        if self._dict is None:
            self._dict = self._request.header_dict
        return self._dict

    def get(self, key: str, default: Any = None) -> Any:
        if self._dict is not None:
            return self._dict.get(key, default)
        value = self._request.header(key)
        return default if value is None else value

    def __getitem__(self, key: str) -> str:
        if self._dict is not None:
            return self._dict[key]
        value = self._request.header(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if self._dict is not None:
            return key in self._dict
        return self._request.header(key) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _LazyHeaders):
            return self._materialize() == other._materialize()
        if isinstance(other, Mapping):
            return self._materialize() == other
        return NotImplemented

    def __repr__(self) -> str:
        return repr(self._materialize())


@dataclass(frozen=True, slots=True)
class Credentials:
    """The parts of a request a provider may inspect."""

    headers: Mapping[str, str]
    query: dict[str, list[str]]
    session: object | None = None


class BaseAuth[T](ABC):
    """Base class of providers.

    ``authenticate`` returns a principal, ``None`` when the request carries no
    credentials, and raises ``Unauthorized`` when credentials are present but
    invalid. Providers hold no per-request state.
    """

    challenge: str | None = None
    uses_session: bool = False
    """``True`` for a provider that reads ``Credentials.session``. The request
    is then given its application session before authentication, and unsafe
    methods carrying a session cookie must pass the session CSRF check. A
    provider that holds others (as attributes, or in a list or tuple) uses the
    session when any of them does."""

    def openapi_scheme(self) -> dict[str, object] | None:
        """OpenAPI security scheme describing this provider, if it has one."""
        return None

    @abstractmethod
    def authenticate(
        self, credentials: Credentials
    ) -> Caller[T] | Awaitable[Caller[T] | None] | None:
        """``async def`` or plain ``def`` (a plain one runs on the sync executor)."""


MAX_AUTH_NESTING = 8


def session_auth(auth: object, depth: int = 0) -> bool:
    """Whether ``auth``, or a provider it is composed of, reads the session."""
    if getattr(auth, "uses_session", False) is True:
        return True
    # Providers written before ``uses_session`` existed.
    if getattr(auth, "__hinoki_session_auth__", False) is True:
        return True
    if depth >= MAX_AUTH_NESTING:
        return False
    return any(session_auth(part, depth + 1) for part in _composed(auth))


def _slot_names(cls: type) -> Iterator[str]:
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        names = (slots,) if isinstance(slots, str) else slots
        yield from (name for name in names if name not in _SLOT_INTERNALS)


_SLOT_INTERNALS = frozenset({"__dict__", "__weakref__"})


def _attribute_values(auth: object) -> list[object]:
    """Instance attribute values, from ``__dict__`` and from ``__slots__``."""
    values = list(getattr(auth, "__dict__", {}).values())
    values.extend(getattr(auth, name, None) for name in _slot_names(type(auth)))
    return values


def _composed(auth: object) -> list[BaseAuth[object]]:
    """The providers ``auth`` holds directly, as attributes or in sequences."""
    values = _attribute_values(auth)
    found: list[BaseAuth[object]] = []
    for value in values:
        if isinstance(value, BaseAuth):
            found.append(value)
        elif isinstance(value, list | tuple | set | frozenset):
            found.extend(item for item in value if isinstance(item, BaseAuth))
    return found


class HeaderAuth[T](BaseAuth[T]):
    """Credential carried in a named header; subclasses implement ``resolve``."""

    header = "authorization"
    _header_lower: ClassVar[str] = "authorization"
    _resolve_fn: ClassVar[object] = None
    _resolve_is_async: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls._header_lower = cls.header.lower()
        cls._resolve_fn = cls.resolve
        cls._resolve_is_async = is_coroutine_hook(cls.resolve)

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "apiKey", "in": "header", "name": self.header}

    @abstractmethod
    def resolve(self, value: str) -> Caller[T] | Awaitable[Caller[T] | None] | None:
        """The principal for a header value or ``None`` if invalid; ``async def``
        or plain ``def``."""

    async def authenticate(self, credentials: Credentials) -> Caller[T] | None:
        cls = type(self)
        header_name = (
            cls._header_lower if self.header == cls.header else self.header.lower()
        )
        value = credentials.headers.get(header_name)
        if value is None:
            return None
        if (
            cls._resolve_is_async
            and cls.resolve is cls._resolve_fn
            and "resolve" not in self.__dict__
        ):
            principal = await cast("Awaitable[Caller[T] | None]", self.resolve(value))
        else:
            principal = await call_hook(self.resolve, value)
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
    _resolve_token_fn: ClassVar[object] = None
    _resolve_token_is_async: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls._resolve_token_fn = cls.resolve_token
        cls._resolve_token_is_async = is_coroutine_hook(cls.resolve_token)

    def openapi_scheme(self) -> dict[str, object] | None:
        return {"type": "http", "scheme": "bearer"}

    @abstractmethod
    def resolve_token(
        self, token: str
    ) -> Caller[T] | Awaitable[Caller[T] | None] | None:
        """``async def`` or plain ``def``, as ``resolve``."""

    async def resolve(self, value: str) -> Caller[T] | None:
        scheme, _, token = value.partition(" ")
        stripped = token.strip()
        if scheme.lower() != "bearer" or not stripped:
            return None
        cls = type(self)
        if (
            cls._resolve_token_is_async
            and cls.resolve_token is cls._resolve_token_fn
            and "resolve_token" not in self.__dict__
        ):
            return await cast(
                "Awaitable[Caller[T] | None]", self.resolve_token(stripped)
            )
        found: Caller[T] | None = await call_hook(self.resolve_token, stripped)
        return found
