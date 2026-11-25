"""The listening socket of the Rust server, opened in Python.

Python opens it (so ``backlog`` is honoured and a supervisor can own it across
worker restarts) and the native server takes the descriptor over. It resolves
and binds like ``std::net::TcpListener::bind``: the first address that works,
``SO_REUSEADDR`` on, ``IPV6_V6ONLY`` left at the operating system default (so a
listener on ``::`` is dual-stack where the system makes it so).
"""

from __future__ import annotations

import os
import socket
import sys

MAX_PORT = 65535


class ListenError(OSError):
    """The listening socket could not be opened (address in use, a bad port or
    host); nothing was served."""


def supports_reuse_port() -> bool:
    """Whether this platform load-balances across ``SO_REUSEPORT`` sockets.

    Only Linux distributes incoming connections across multiple ``SO_REUSEPORT``
    listeners in the kernel; macOS and other BSDs do not, so they keep a single
    shared listening descriptor instead.
    """
    return sys.platform == "linux" and hasattr(socket, "SO_REUSEPORT")


def check_port(port: int) -> None:
    """Refuse a port outside 0 to 65535. The system's resolver does not: glibc
    wraps a larger number to 16 bits, so ``70000`` would quietly listen on 4464."""
    if not 0 <= port <= MAX_PORT:
        raise ListenError(f"invalid port {port}: it must be 0 to {MAX_PORT}")


def open_listener(
    host: str, port: int, backlog: int, *, reuse_port: bool = False
) -> socket.socket:
    """A bound, listening TCP socket for ``host:port`` (port 0 picks a free one).

    When ``reuse_port`` is true (Linux only), sets ``SO_REUSEPORT`` before
    binding so each worker process can own its own accept queue.
    """
    check_port(port)
    if reuse_port and not supports_reuse_port():
        raise ListenError("SO_REUSEPORT is only supported on Linux")
    failure: OSError | None = None
    try:
        candidates = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except (OSError, OverflowError, ValueError) as error:  # bad port or name
        raise ListenError(f"invalid address: {error}") from error
    for family, kind, protocol, _, address in candidates:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if reuse_port:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(address)
            sock.listen(backlog)
        except (OverflowError, ValueError, UnicodeError) as error:
            sock.close()
            raise ListenError(f"invalid address: {error}") from error
        except OSError as error:
            sock.close()
            failure = error
            continue
        return sock
    raise ListenError(
        failure.errno if failure else None,
        failure.strerror if failure else f"cannot resolve {host}",
    ) from failure


def is_listening(sock: socket.socket) -> bool:
    """Whether ``listen`` was called on it; true where the system cannot say
    (macOS answers ``SO_ACCEPTCONN`` with an error)."""
    try:
        return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except OSError:
        return True


def inherited_address(fd: int) -> tuple[str, int]:
    """The ``(host, port)`` of an inherited listening descriptor. The socket
    object is detached again, so the descriptor stays open for the native
    server. Raises ``OSError`` when ``fd`` is not a listening TCP socket."""
    sock = socket.socket(fileno=fd)
    try:
        if sock.type != socket.SOCK_STREAM or not is_listening(sock):
            raise OSError(f"descriptor {fd} is not a listening TCP socket")
        host, port = sock.getsockname()[:2]
    finally:
        sock.detach()
    return str(host), int(port)


def signal_ready(fd: int) -> None:
    """Tell the supervisor that this worker serves: one byte on its pipe, then
    close it. A worker that fails closes the pipe without the byte."""
    try:
        os.write(fd, b"1")
    finally:
        os.close(fd)
