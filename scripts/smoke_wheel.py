"""Smoke-test an installed Hinoki wheel: import it and serve a minimal app.

Run it with the interpreter of a clean environment that only has the wheel
installed: ``/path/to/venv/bin/python scripts/smoke_wheel.py``.
"""

import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

import hinoki
from hinoki import core

APP = textwrap.dedent(
    """
    from hinoki import Hinoki, Schema, http


    class Health(Schema):
        status: str


    @http.get("/health")
    def health() -> Health:
        return Health(status="ok")


    app = Hinoki(include=[health])
    """
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(url: str, attempts: int = 50) -> bytes:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as reply:  # noqa: S310
                return bytes(reply.read())
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"no answer from {url}")


def main() -> None:
    if core.Router.__module__ != "hinoki.core":
        raise SystemExit("hinoki.core is not the native extension")
    print(f"hinoki {hinoki.__version__} from {Path(hinoki.__file__).parent}")
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "smokeapp.py").write_text(APP)
        port = free_port()
        command = [sys.executable, "-m", "hinoki", "serve", "smokeapp:app"]
        server = subprocess.Popen(  # noqa: S603
            [*command, "--port", str(port)], cwd=directory
        )
        try:
            body = fetch(f"http://127.0.0.1:{port}/health")
        finally:
            server.terminate()
            server.wait(timeout=30)
    if body != b'{"status":"ok"}':
        raise SystemExit(f"unexpected answer: {body!r}")
    print("wheel smoke ok")


if __name__ == "__main__":
    main()
