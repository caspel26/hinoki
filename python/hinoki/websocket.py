"""WebSocket actions: ``@http.websocket("/rooms/{room}")``.

    from hinoki import http

    @http.websocket("/echo")
    async def echo(socket: http.WebSocket) -> None:
        async for text in socket:
            await socket.send_text(text)

The handshake is an HTTP request: ``access`` runs before the connection is
upgraded, so a refusal is the usual ``401`` or ``403`` and the handler never
starts. Framing, ping and pong, close, size limits and back-pressure run in the
Rust server (Kiso); the handler only sees whole messages.
"""

from __future__ import annotations

import inspect
import math
import re
import types
import typing
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Final, ParamSpec, Protocol, TypeVar, overload

from hinoki import core
from hinoki.access import Access
from hinoki.auth import Caller
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fields import UNSET, Unset, carries_field
from hinoki.schema import Schema, encode

S = TypeVar("S", bound=Schema)
P = ParamSpec("P")
R = TypeVar("R")

WS_ATTR: Final = "__hinoki_websocket__"
SCALARS: Final = (str, int, float, bool, uuid.UUID)
PATH_PARAM: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ORIGIN: Final = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")
TOKEN: Final = re.compile(r"[A-Za-z0-9!#$%&'*+\-.^_`|~]+")
ABNORMAL: Final = 1006


class CloseCode(IntEnum):
    """The status codes a handler is likely to send (RFC 6455, section 7.4)."""

    NORMAL = 1000
    GOING_AWAY = 1001
    PROTOCOL_ERROR = 1002
    UNSUPPORTED_DATA = 1003
    INVALID_DATA = 1007
    POLICY_VIOLATION = 1008
    MESSAGE_TOO_BIG = 1009
    INTERNAL_ERROR = 1011
    TRY_AGAIN_LATER = 1013


class WebSocketClosed(Exception):
    """The connection is closed: the peer closed it, the server did (shutdown,
    idle timeout, a limit), or the handler did. ``code`` and ``reason`` are the
    close the connection ended with (``1006`` when it was lost without one)."""

    def __init__(self, code: int = ABNORMAL, reason: str = "") -> None:
        super().__init__(f"WebSocket closed ({code})")
        self.code = code
        self.reason = reason


class Transport(Protocol):
    """What a server gives a ``WebSocket``: Kiso's object, or the ASGI adapter."""

    def receive(self) -> Awaitable[tuple[str, str | bytes] | None]: ...

    def send_text(self, text: str) -> Awaitable[bool]: ...

    def send_bytes(self, data: bytes) -> Awaitable[bool]: ...

    def close(self, code: int = 1000, reason: str = "") -> Awaitable[bool]: ...

    @property
    def closed(self) -> tuple[int, str] | None: ...


class WebSocket:
    """One open WebSocket, as the handler sees it.

    ``receive_*`` wait for the next message and raise ``WebSocketClosed`` once the
    connection is over; ``send_*`` wait for room in the bounded outgoing queue.
    ``async for message in socket`` ends when the connection does."""

    def __init__(
        self,
        transport: Transport,
        *,
        path: str,
        subprotocol: str | None = None,
        message: type[Schema] | None = None,
        reply: type[Schema] | None = None,
    ) -> None:
        self.transport = transport
        self.path = path
        self.subprotocol = subprotocol
        self.message = message
        self.reply = reply

    @property
    def closed(self) -> tuple[int, str] | None:
        """``(code, reason)`` once the connection is over, else ``None``."""
        return self.transport.closed

    async def receive_text(self) -> str:
        """The next message, which must be text; a binary one closes with 1003."""
        kind, payload = await self._next()
        if kind != "text":
            await self.close(CloseCode.UNSUPPORTED_DATA, "text expected")
            raise WebSocketClosed(CloseCode.UNSUPPORTED_DATA, "text expected")
        return typing.cast("str", payload)

    async def receive_bytes(self) -> bytes:
        """The next message, which must be binary; a text one closes with 1003."""
        kind, payload = await self._next()
        if kind != "bytes":
            await self.close(CloseCode.UNSUPPORTED_DATA, "binary expected")
            raise WebSocketClosed(CloseCode.UNSUPPORTED_DATA, "binary expected")
        return typing.cast("bytes", payload)

    @overload
    async def receive_json(self) -> Any: ...

    @overload
    async def receive_json(self, schema: type[S]) -> S: ...

    async def receive_json(self, schema: type[Schema] | None = None) -> Any:
        """The next text message parsed as JSON and, with a ``Schema`` (the
        argument, else the route's ``message``), validated. Text that is not JSON
        closes with 1007; JSON that does not validate closes with 1008."""
        return await self._decode(await self.receive_text(), schema or self.message)

    async def send_text(self, text: str) -> None:
        if not await self.transport.send_text(text):
            raise self._gone()

    async def send_bytes(self, data: bytes) -> None:
        if not await self.transport.send_bytes(bytes(data)):
            raise self._gone()

    async def send_json(self, value: object) -> None:
        """Send ``value`` as a JSON text message. With ``reply=`` on the route a
        ``dict`` is validated into that ``Schema`` first, and an instance of a
        subclass of it is sent with the fields of ``reply`` only."""
        reply = self.reply
        if reply is not None and type(value) is not reply:
            value = self._as_reply(reply, value)
        await self.send_text(
            core.encode_json(encode(value), None, False, None).decode()
        )

    @staticmethod
    def _as_reply(reply: type[Schema], value: object) -> object:
        if not isinstance(value, reply):
            return reply.validate(value)
        narrowed = core.dump_as(value, reply)
        return value.dump(include=reply.specs()) if narrowed is None else narrowed

    async def close(self, code: int = CloseCode.NORMAL, reason: str = "") -> None:
        """Close after the messages already queued. ``ValueError`` for a code that
        a peer may not send (``1005``, ``1006``, ``1015`` and anything outside 1000
        to 4999); closing a closed connection does nothing."""
        await self.transport.close(int(code), reason)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[Any]:
        while True:
            try:
                kind, payload = await self._next()
            except WebSocketClosed:
                return
            if self.message is None:
                yield payload
            elif kind == "text":
                yield await self._decode(typing.cast("str", payload), self.message)
            else:
                await self.close(CloseCode.UNSUPPORTED_DATA, "text expected")
                return

    async def _next(self) -> tuple[str, str | bytes]:
        item = await self.transport.receive()
        if item is None:
            raise self._gone()
        return item

    def _gone(self) -> WebSocketClosed:
        found = self.transport.closed
        if found is None:
            return WebSocketClosed()
        return WebSocketClosed(*found)

    async def _decode(self, text: str, schema: type[Schema] | None) -> Any:
        try:
            value = core.decode_json(text.encode())
        except core.JsonDecodeError:
            await self.close(CloseCode.INVALID_DATA, "invalid JSON")
            raise WebSocketClosed(CloseCode.INVALID_DATA, "invalid JSON") from None
        if schema is None:
            return value
        try:
            return schema.validate(value)
        except ValidationError:
            await self.close(CloseCode.POLICY_VIOLATION, "invalid message")
            raise WebSocketClosed(
                CloseCode.POLICY_VIOLATION, "invalid message"
            ) from None


@dataclass(frozen=True, slots=True)
class Param:
    """A handler parameter other than the socket and the caller."""

    name: str
    hint: type[Any]
    path: bool
    required: bool
    default: object = None


@dataclass(frozen=True, slots=True)
class WebSocketRoute:
    """A declared WebSocket action."""

    path: str
    handler: Callable[..., Awaitable[object]]
    access: Access | None
    message: type[Schema] | None
    reply: type[Schema] | None
    subprotocols: tuple[tuple[str, Access | None], ...]
    origins: tuple[str, ...]
    max_message_bytes: int | None
    idle_timeout: float | None
    # Read from the handler's signature when the application includes the
    # route (``bound``), so annotations may be forward references until then.
    params: tuple[Param, ...] = ()
    socket: str = ""
    caller: str | None = None
    owner: type | None = None  # the ``API`` class of a method, made per connection
    bound: bool = False
    access_explicit: bool = False

    @property
    def name(self) -> str:
        return f"{self.handler.__module__}.{self.handler.__qualname__}"

    def resolved(self) -> WebSocketRoute:
        """The route with its parameters read from the handler's signature."""
        if self.bound:
            return self
        socket, caller, params = bind_signature(self)
        return replace(self, socket=socket, caller=caller, params=params, bound=True)


def websocket_of(obj: object) -> WebSocketRoute | None:
    """The WebSocket route a decorated function declares."""
    if not callable(obj):
        return None
    found = getattr(obj, WS_ATTR, None)
    if not isinstance(found, WebSocketRoute):
        return None
    return found if found.handler is obj else replace(found, handler=obj)


def websocket(  # noqa: PLR0913 - one keyword per documented option
    path: str,
    *,
    access: Access | Unset | None = UNSET,
    message: type[Schema] | None = None,
    reply: type[Schema] | None = None,
    subprotocols: Sequence[str] | Mapping[str, Access | None] = (),
    origins: Sequence[str] = (),
    max_message_bytes: int | None = None,
    idle_timeout: float | None = None,
) -> Callable[
    [Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]
]:
    """Declare a WebSocket action on ``path`` (``/rooms/{room}``), on a function
    or on a method of an ``API`` class (whose prefix it then takes).

    ``access`` authenticates the handshake; a refusal is the usual HTTP answer.
    ``message`` and ``reply`` are the ``Schema`` of the JSON the client sends and
    receives. ``subprotocols`` names the ones the action speaks (the client must
    offer one); a mapping gives each its own ``Access``. ``origins`` adds allowed
    browser origins to the same-origin and application CORS ones.
    ``max_message_bytes`` and ``idle_timeout`` override the ``execution.ws_*``
    settings; ``idle_timeout`` (seconds without a message from the client) is off
    by default. The handler's annotations are read when the application
    includes it, so they may name classes defined later."""
    checked = check_options(
        path, message, reply, subprotocols, origins, max_message_bytes, idle_timeout
    )
    resolved_access = None if isinstance(access, Unset) else access
    access_explicit = not isinstance(access, Unset)

    def decorate(
        fn: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        if hasattr(fn, WS_ATTR):
            raise RegistrationError(f"{fn.__qualname__} already declares a WebSocket")
        if not inspect.iscoroutinefunction(fn):
            raise RegistrationError(f"{fn.__qualname__} must be an async def")
        declared = WebSocketRoute(
            path,
            fn,
            resolved_access,
            message,
            reply,
            checked[0],
            checked[1],
            max_message_bytes,
            idle_timeout,
            access_explicit=access_explicit,
        )
        setattr(fn, WS_ATTR, declared)
        return fn

    return decorate


def check_options(
    path: str,
    message: type[Schema] | None,
    reply: type[Schema] | None,
    subprotocols: Sequence[str] | Mapping[str, Access | None],
    origins: Sequence[str],
    max_message_bytes: int | None,
    idle_timeout: float | None,
) -> tuple[tuple[tuple[str, Access | None], ...], tuple[str, ...]]:
    if not path.startswith("/") or "?" in path or "#" in path:
        raise RegistrationError(
            f"a WebSocket path starts with / and has no query: {path!r}"
        )
    for schema in (message, reply):
        if schema is not None and not (
            isinstance(schema, type) and issubclass(schema, Schema)
        ):
            raise RegistrationError("message=... and reply=... take a Schema class")
    if max_message_bytes is not None and max_message_bytes < 1:
        raise RegistrationError("max_message_bytes must be positive")
    if idle_timeout is not None and idle_timeout <= 0:
        raise RegistrationError("idle_timeout must be positive")
    return check_subprotocols(subprotocols), check_origins(origins)


def check_subprotocols(
    subprotocols: Sequence[str] | Mapping[str, Access | None],
) -> tuple[tuple[str, Access | None], ...]:
    if isinstance(subprotocols, str):
        raise RegistrationError("subprotocols is a list of names, not one string")
    items = (
        list(subprotocols.items())
        if isinstance(subprotocols, Mapping)
        else [(name, None) for name in subprotocols]
    )
    for name, access in items:
        if not TOKEN.fullmatch(name):
            raise RegistrationError(
                f"not a subprotocol name (an HTTP token, e.g. 'chat.v1'): {name!r}"
            )
        if access is not None and not isinstance(access, Access):
            raise RegistrationError("a subprotocol takes an Access or None")
    return tuple(items)


def check_origins(origins: Sequence[str]) -> tuple[str, ...]:
    if isinstance(origins, str):
        raise RegistrationError("origins is a list, not one string")
    for origin in origins:
        if origin != "*" and not ORIGIN.match(origin):
            raise RegistrationError(
                f"origins are '*' or scheme://host[:port], not {origin!r}"
            )
    return tuple(origin.lower() for origin in origins)


def handler_hints(
    fn: Callable[..., object], include_extras: bool = False
) -> dict[str, Any]:
    try:
        return typing.get_type_hints(fn, include_extras=include_extras)
    except Exception as exc:
        raise RegistrationError(
            f"{fn.__qualname__}: cannot resolve annotations: {exc}"
        ) from exc


def handler_parameters(route: WebSocketRoute) -> list[inspect.Parameter]:
    """The parameters the server fills: every one after ``self`` on a method."""
    parameters = list(inspect.signature(route.handler).parameters.values())
    if route.owner is None:
        return parameters
    required = [
        p.name
        for p in inspect.signature(route.owner).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    ]
    if required:
        raise RegistrationError(
            f"{route.owner.__name__} is created without arguments per connection; "
            f"its __init__ requires {required}"
        )
    return parameters[1:]


def bind_signature(
    route: WebSocketRoute,
) -> tuple[str, str | None, tuple[Param, ...]]:
    """The socket and caller parameter names, and the others as ``Param``."""
    fn, path, access = route.handler, route.path, route.access
    hints = handler_hints(fn)
    # a second read that keeps ``Annotated`` metadata, to refuse ``Field(...)``
    extras = handler_hints(fn, include_extras=True)
    in_path = set(PATH_PARAM.findall(path))
    socket: str | None = None
    caller: str | None = None
    params: list[Param] = []
    for parameter in handler_parameters(route):
        name = parameter.name
        hint = hints.get(name, str)
        if hint is WebSocket:
            socket = name
        elif typing.get_origin(hint) is Caller or hint is Caller:
            caller = name
        else:
            if carries_field(extras.get(name)):
                raise RegistrationError(
                    f"{fn.__qualname__}: `Field(...)` on the scalar parameter "
                    f"{name!r} is not enforced, and its description and examples are "
                    "not read either; validate it inside the handler or take a "
                    "`Schema`"
                )
            params.append(scalar_param(fn, name, hint, parameter, name in in_path))
    if socket is None:
        raise RegistrationError(
            f"{fn.__qualname__} needs a parameter annotated http.WebSocket"
        )
    if (
        caller is not None
        and access is None
        and not any(a for _, a in route.subprotocols)
    ):
        raise RegistrationError(
            f"{fn.__qualname__} takes a Caller but declares no access"
        )
    missing = in_path - {param.name for param in params}
    if missing:
        raise RegistrationError(
            f"{fn.__qualname__} lacks a parameter for {sorted(missing)} in {path}"
        )
    return socket, caller, tuple(params)


def scalar_param(
    fn: Callable[..., object],
    name: str,
    hint: object,
    parameter: inspect.Parameter,
    path: bool,
) -> Param:
    args = [arg for arg in typing.get_args(hint) if arg is not type(None)]
    optional = typing.get_origin(hint) in (typing.Union, types.UnionType)
    plain = args[0] if optional and len(args) == 1 else hint
    if plain not in SCALARS:
        raise RegistrationError(
            f"{fn.__qualname__}: parameter {name!r} must be "
            "str, int, float, bool or UUID"
        )
    required = parameter.default is inspect.Parameter.empty and plain is hint
    return Param(
        name,
        typing.cast("type[Any]", plain),
        path,
        required,
        None if parameter.default is inspect.Parameter.empty else parameter.default,
    )


def convert(param: Param, text: str) -> object:
    """A path or query value as the parameter's type; ``ValueError`` when it
    does not read as one."""
    if param.hint is bool:
        lowered = text.lower()
        if lowered not in {"true", "false", "1", "0"}:
            raise ValueError(text)
        return lowered in {"true", "1"}
    value = param.hint(text)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(text)  # as HTTP parameters: no nan or inf
    return value
