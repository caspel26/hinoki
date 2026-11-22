---
status: implemented
related:
  - endpoints.md
  - kiso.md
  - streaming.md
  - openapi.md
  - settings.md
---

# WebSockets

`@ws.route` declares a WebSocket action the way `@http.get` declares an HTTP one.
The handler is an `async def` that receives a `Socket` and exchanges whole
messages with the client: text, binary or JSON checked against a `Schema`.

```python
# chat/echo.py
from hinoki import ws


@ws.route("/echo")
async def echo(socket: ws.Socket) -> None:
    async for text in socket:
        await socket.send_text(text)
```

Include it like any other action (`Hinoki(include=[echo])`, `routes.py`, `Mount`).
The connection ends when the handler returns (close code `1000`), when the
client closes, or when the handler raises (`1011`).

## What runs where

On Kiso, the WebSocket protocol runs in Rust: the handshake, frame parsing and
masking, text validation, fragmentation, ping and pong, the close handshake,
the size limits and the timers. The handler only sees whole messages through
two bounded queues; Python runs the handler and, at the handshake, `access` and
the argument binding. Messages cross the Python boundary as `str` and `bytes`.
WebSocket over HTTP/2, `permessage-deflate` and other extensions are not
provided; a client that offers extensions gets none.

Under Granian (any ASGI server) the same actions run through the ASGI
`websocket` scope. The server owns the frames, so Hinoki enforces only what a
Python layer can: the Origin check, the message size and the handler lifecycle.
The ping interval, idle timeout, `max_websockets` and the queue size do not
apply; use the server's own options.

## The handshake

The handshake is an HTTP `GET`, so the usual answers apply **before** the
connection is upgraded and the handler never starts for a refused client:

| Answer | When |
| --- | --- |
| `426` | the request is not a WebSocket upgrade, or asks for a version other than 13 (the answer names `Sec-WebSocket-Version: 13`) |
| `400` | an invalid `Sec-WebSocket-Key`, an unsupported subprotocol, or a path or query value that is not valid |
| `401`, `403` | `access` refused the caller |
| `403` | the `Origin` is not allowed |
| `503` | `execution.max_websockets` connections are open |

A WebSocket path may not also be an HTTP `GET` route with the same pattern. On
Kiso a plain `GET` that a more specific HTTP route matches (`/rooms/stats`
beside `/rooms/{room}`) goes to the HTTP route; only upgrade requests reach the
WebSocket action. A path with a
parameter is declared as in HTTP: `@ws.route("/rooms/{room}")`. Path and query
values are bound to handler parameters typed `str`, `int`, `float`, `bool` or
`UUID`; a parameter with a default is optional. A malformed value is refused
with `400` before the upgrade.

## Access and the caller

`access=` takes the same reusable `Access` as an HTTP action and is judged on
the handshake request: its headers, cookies and query string. A browser cannot
set an `Authorization` header on a WebSocket, so use a cookie or a provider that
reads the query. A parameter annotated `Caller` receives the authenticated
caller, as in HTTP. Authorization of what each message may do stays in your
handler, which is also where tenant or ownership checks belong; WebSocket
actions are not part of generated CRUD.

```python
from hinoki import Access, Caller, ws

READ = Access(tokens, permissions=["chat:read"])


@ws.route("/inbox", access=READ)
async def inbox(who: Caller[None], socket: ws.Socket) -> None:
    await socket.send_text(f"hello {who.id}")
```

## Origin

A request without an `Origin` header (not a browser) is accepted. A request
with one must match the host the request was sent to, an origin listed in
`origins=` on the route (`origins=["https://app.example"]`, or `"*"`), or an
origin the application's CORS policy lists by name. A CORS wildcard
(`cors` origins `*`) is not honoured here: a browser sends cookies on a
WebSocket handshake and does not hide the answer from other sites, so an open
policy would let any site use a cookie-authenticated action. Anything else is
refused with `403` before the upgrade. On Granian the check covers the route
list and the same host.

## Subprotocols

`subprotocols=` names the subprotocols an action speaks, each an HTTP token
(letters, digits and ``!#$%&'*+-.^_`|~``, so `chat.v1` but not `chat/v1`). The server picks the
first one the client offered and returns it in the `101` answer
(`socket.subprotocol`). A client that offers none of them is refused with `400`.
A mapping gives each subprotocol its own `Access`, which runs after the
action's:

```python
@ws.route("/feed", subprotocols={"public.v1": None, "staff.v1": STAFF})
async def feed(socket: ws.Socket) -> None:
    await socket.send_text(socket.subprotocol or "")
```

## Messages

| Call | Result |
| --- | --- |
| `await socket.receive_text()` | the next text message; a binary one closes with `1003` |
| `await socket.receive_bytes()` | the next binary message; a text one closes with `1003` |
| `await socket.receive_json(schema=None)` | text parsed as JSON, then validated against the `Schema` given or the route's `message` |
| `await socket.send_text(text)`, `send_bytes(data)` | queue a message, waiting while the outgoing queue is full |
| `await socket.send_json(value)` | a JSON text message; with `reply=` on the route, validated against it |
| `await socket.close(code=1000, reason="")` | close after the queued messages |
| `async for message in socket` | each message, until the connection ends; with `message=` on the route, each is a validated `Schema` instance |

Reading or sending on a closed connection raises `ws.Closed` (with `code` and
`reason`); a handler may let it propagate, since it is not an error. Text that
is not JSON closes with `1007`, JSON that fails validation with `1008`.

```python
from hinoki import Schema, ws


class Move(Schema):
    x: int
    y: int


class Position(Schema):
    x: int
    y: int
    by: str


@ws.route("/board", message=Move, reply=Position)
async def board(socket: ws.Socket) -> None:
    async for move in socket:
        await socket.send_json({"x": move.x + 1, "y": move.y, "by": "server"})
```

## Limits and back-pressure

Settings under `execution` (see [settings](settings.md)); each is enforced by
Kiso:

| Setting | Default | Effect |
| --- | --- | --- |
| `max_websockets` | `1000` | open connections per worker; more are refused with `503` before the upgrade |
| `ws_max_message_bytes` | `1048576` | a message (reassembled from its frames) over this closes with `1009`; `max_message_bytes=` on the route overrides it |
| `ws_queue` | `64` | messages queued in each direction; a slow handler stops being read from, so TCP back-pressure reaches the client, and `send_*` waits while the client is slow |
| `ws_ping_interval` | `20.0` | seconds between pings; `None` turns them off |
| `ws_pong_timeout` | `20.0` | a peer that does not answer a ping within this is dropped (reported to the handler as `1006`) |
| `ws_close_timeout` | `5.0` | how long the close handshake may take before the connection is dropped |

`idle_timeout=` on the route (seconds without a data message from the client,
closing with `1001`) is off by default, since a server-push connection can be
silent from the client on purpose. Frames that are malformed, unmasked from a
client, reserved or oversized control frames, invalid UTF-8 in text, and bad
close codes end the connection with `1002` or `1007` as RFC 6455 requires. A
write that makes no progress for `execution.request_timeout` seconds drops the
connection.

## Shutdown and reload

When the server stops, Kiso closes every open connection with `1001` and the
handler's `receive` and `send` calls end (`async for` stops, the explicit calls
raise `ws.Closed`). Connections are part of the graceful drain, so they
get the shutdown timeout to finish and are aborted after it. A `SIGHUP`
reload replaces a worker through the same graceful stop, so its connections
get `1001` and a client is expected to reconnect, which reaches the new worker.

## OpenAPI

OpenAPI 3.1 has no WebSocket operation, so the document describes the actions
under the vendor extension `x-hinoki-websockets`, keyed by path: the handler
docstring, the `message` and `reply` schema components, the subprotocols, the
origins and the handshake's security requirement. They are not listed under
`paths`.

## Not provided

WebSocket over HTTP/2, `permessage-deflate`, class-based `@ws.route` on an
`API`, and a native (GIL-free) handler lane for WebSockets.
