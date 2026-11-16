"""The listening socket of the Rust server, opened in Python.

Python opens it (so ``backlog`` is honoured and a supervisor can own it across
worker restarts) and the native server takes the descriptor over. It resolves
and binds like ``std::net::TcpListener::bind``: the first address that works,
``SO_REUSEADDR`` on, ``IPV6_V6ONLY`` left at the operating system default (so a
listener on ``::`` is dual-stack where the system makes it so).
"""

from __future__ import annotations

import socket


def open_listener(host: str, port: int, backlog: int) -> socket.socket:
    """A bound, listening TCP socket for ``host:port`` (port 0 picks a free one)."""
    failure: OSError | None = None
    candidates = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )
    for family, kind, protocol, _, address in candidates:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(address)
            sock.listen(backlog)
        except OSError as error:
            sock.close()
            failure = error
            continue
        return sock
    raise failure or OSError(f"cannot resolve {host}")


def inherited_address(fd: int) -> tuple[str, int]:
    """The ``(host, port)`` of an inherited listening descriptor. The socket
    object is detached again, so the descriptor stays open for the native
    server. Raises ``OSError`` when ``fd`` is not a socket."""
    sock = socket.socket(fileno=fd)
    try:
        host, port = sock.getsockname()[:2]
    finally:
        sock.detach()
    return str(host), int(port)
