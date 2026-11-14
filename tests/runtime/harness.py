"""Helpers to run an ASGI target under a real Granian process."""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server(subprocess.Popen[bytes]):
    """A Granian process in its own process group. ``kill`` ends the whole
    group: killing only the supervisor would leave its workers running after
    the test."""

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGKILL)


def group_is_gone(pid: int) -> bool:
    """No process of the group ``pid`` led is left (zombies aside)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def spawn(
    target: str,
    log: Path,
    port: int,
    workers: int = 1,
    factory: bool = False,
    cwd: Path = HERE,
    **env: str,
) -> Server:
    environment = {**os.environ, "EXP_LOG": str(log), **env}
    command = [
        sys.executable, "-m", "granian", "--interface", "asgi",
        "--workers", str(workers), "--port", str(port),
        "--no-log", *(["--factory"] if factory else []), target,
    ]  # fmt: skip
    return Server(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )


def events(log: Path) -> list[tuple[str, str]]:
    """Recorded ``(pid, event)`` pairs in order."""
    if not log.exists():
        return []
    rows = [line.split() for line in log.read_text().splitlines()]
    return [(row[1], row[2]) for row in rows]


def names(log: Path) -> list[str]:
    return [event for _, event in events(log)]


def fetch(port: int, path: str = "/") -> str:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return str(response.read().decode())


def wait_ready(port: int, path: str = "/") -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            fetch(port, path)
        except OSError:
            time.sleep(0.1)
        else:
            return
    raise AssertionError("server did not become ready")
