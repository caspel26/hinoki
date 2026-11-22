"""WebSocket actions: ``@ws.route("/rooms/{room}")``.

    from hinoki import ws

    @ws.route("/echo")
    async def echo(socket: ws.Socket) -> None:
        async for text in socket:
            await socket.send_text(text)

The handshake is an HTTP request: ``access`` runs before the connection is
upgraded, so a refusal is the usual ``401`` or ``403`` and the handler never
starts. Framing, ping and pong, close, size limits and back-pressure run in the
Rust server (Kiso); the handler only sees whole messages.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Final, Protocol, TypeVar, overload

from hinoki import core
from hinoki.access import Access
from hinoki.auth import Caller
from hinoki.errors import RegistrationError, ValidationError
from hinoki.schema import Schema, encode

S = TypeVar("S", bound=Schema)

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


class Closed(Exception):
    """The connection is closed: the peer closed it, the server did (shutdown,
    idle timeout, a limit), or the handler did. ``code`` and ``reason`` are the
    close the connection ended with (``1006`` when it was lost without one)."""

    def __init__(self, code: int = ABNORMAL, reason: str = "") -> None:
        super().__init__(f"WebSocket closed ({code})")
        self.code = code
        self.reason = reason


class Transport(Protocol):
    """What a server gives a ``Socket``: Kiso's native object, or the ASGI adapter."""

    def receive(self) -> Awaitable[tuple[str, str | bytes] | None]: ...

    def send_text(self, text: str) -> Awaitable[bool]: ...

    def send_bytes(self, data: bytes) -> Awaitable[bool]: ...

    def close(self, code: int = 1000, reason: str = "") -> Awaitable[bool]: ...

    @property
    def closed(self) -> tuple[int, str] | None: ...


class Socket:
    """One open WebSocket, as the handler sees it.

    ``receive_*`` wait for the next message and raise ``Closed`` once the
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
            raise Closed(CloseCode.UNSUPPORTED_DATA, "text expected")
        return typing.cast("str", payload)

    async def receive_bytes(self) -> bytes:
        """The next message, which must be binary; a text one closes with 1003."""
        kind, payload = await self._next()
        if kind != "bytes":
            await self.close(CloseCode.UNSUPPORTED_DATA, "binary expected")
            raise Closed(CloseCode.UNSUPPORTED_DATA, "binary expected")
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
        ``dict`` is validated into that ``Schema`` first."""
        if self.reply is not None and not isinstance(value, self.reply):
            value = self.reply.validate(value)
        await self.send_text(
            core.encode_json(encode(value), None, False, None).decode()
        )

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
            except Closed:
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

    def _gone(self) -> Closed:
        found = self.transport.closed
        return Closed(*found) if found is not None else Closed()

    async def _decode(self, text: str, schema: type[Schema] | None) -> Any:
        try:
            value = core.decode_json(text.encode())
        except core.JsonDecodeError:
            await self.close(CloseCode.INVALID_DATA, "invalid JSON")
            raise Closed(CloseCode.INVALID_DATA, "invalid JSON") from None
        if schema is None:
            return value
        try:
            return schema.validate(value)
        except ValidationError:
            await self.close(CloseCode.POLICY_VIOLATION, "invalid message")
            raise Closed(CloseCode.POLICY_VIOLATION, "invalid message") from None


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
    params: tuple[Param, ...]
    socket: str
    caller: str | None

    @property
    def name(self) -> str:
        return f"{self.handler.__module__}.{self.handler.__qualname__}"


def websocket_of(obj: object) -> WebSocketRoute | None:
    """The WebSocket route a decorated function declares."""
    if not callable(obj):
        return None
    found = getattr(obj, WS_ATTR, None)
    if not isinstance(found, WebSocketRoute):
        return None
    return found if found.handler is obj else replace(found, handler=obj)


def route(  # noqa: PLR0913 - one keyword per documented option
    path: str,
    *,
    access: Access | None = None,
    message: type[Schema] | None = None,
    reply: type[Schema] | None = None,
    subprotocols: Sequence[str] | Mapping[str, Access | None] = (),
    origins: Sequence[str] = (),
    max_message_bytes: int | None = None,
    idle_timeout: float | None = None,
) -> Callable[[Callable[..., Awaitable[object]]], Callable[..., Awaitable[object]]]:
    """Declare a WebSocket action on ``path`` (``/rooms/{room}``).

    ``access`` authenticates the handshake; a refusal is the usual HTTP answer.
    ``message`` and ``reply`` are the ``Schema`` of the JSON the client sends and
    receives. ``subprotocols`` names the ones the action speaks (the client must
    offer one); a mapping gives each its own ``Access``. ``origins`` adds allowed
    browser origins to the same-origin and application CORS ones.
    ``max_message_bytes`` and ``idle_timeout`` override the ``execution.ws_*``
    settings; ``idle_timeout`` (seconds without a message from the client) is off
    by default."""
    checked = check_options(
        path, message, reply, subprotocols, origins, max_message_bytes, idle_timeout
    )

    def decorate(
        fn: Callable[..., Awaitable[object]],
    ) -> Callable[..., Awaitable[object]]:
        if hasattr(fn, WS_ATTR):
            raise RegistrationError(f"{fn.__qualname__} already declares a WebSocket")
        if not inspect.iscoroutinefunction(fn):
            raise RegistrationError(f"{fn.__qualname__} must be an async def")
        socket, caller, params = bind_signature(fn, path, access, checked[0])
        declared = WebSocketRoute(
            path,
            fn,
            access,
            message,
            reply,
            checked[0],
            checked[1],
            max_message_bytes,
            idle_timeout,
            params,
            socket,
            caller,
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


def bind_signature(
    fn: Callable[..., object],
    path: str,
    access: Access | None,
    subprotocols: tuple[tuple[str, Access | None], ...],
) -> tuple[str, str | None, tuple[Param, ...]]:
    """The socket and caller parameter names, and the others as ``Param``."""
    try:
        hints = typing.get_type_hints(fn)
    except Exception as exc:
        raise RegistrationError(
            f"{fn.__qualname__}: cannot resolve annotations: {exc}"
        ) from exc
    in_path = set(PATH_PARAM.findall(path))
    socket: str | None = None
    caller: str | None = None
    params: list[Param] = []
    for name, parameter in inspect.signature(fn).parameters.items():
        hint = hints.get(name, str)
        if hint is Socket:
            socket = name
        elif typing.get_origin(hint) is Caller or hint is Caller:
            caller = name
        else:
            params.append(scalar_param(fn, name, hint, parameter, name in in_path))
    if socket is None:
        raise RegistrationError(
            f"{fn.__qualname__} needs a parameter annotated ws.Socket"
        )
    if caller is not None and access is None and not any(a for _, a in subprotocols):
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
    return param.hint(text)
