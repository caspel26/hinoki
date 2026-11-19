"""The process supervisor of the Rust server: ``hinoki serve --workers N`` and
``hinoki dev``.

The supervisor opens the listening socket once and starts every worker as a
fresh process (``python -m hinoki.runtime.kiso``, with the descriptor
passed down; no ``fork``), so no tokio runtime, database pool or request-id
prefix is inherited: each worker imports the target, builds the application and
starts its runtime after it starts. The supervisor never starts tokio itself.

* Stop: SIGTERM or SIGINT sends SIGTERM to every worker, which stops gracefully
  (running requests finish up to ``shutdown_timeout``). Workers still alive after
  the grace period are killed; a second signal kills them at once.
* A worker that exits while the supervisor is not stopping is not replaced: the
  others are stopped and the supervisor exits non-zero (a startup failure and a
  crash are handled the same way). Under ``dev`` the worker is instead restarted
  when a source file changes, on the same socket, so connections wait in the
  backlog during the restart.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

POLL = 0.25
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# Seconds past ``shutdown_timeout`` before a worker is killed.
KILL_MARGIN = 5.0


class Supervisor:
    """Owns the listener and the worker processes of one ``serve`` or ``dev`` run."""

    def __init__(
        self,
        target: str,
        listener: socket.socket,
        workers: int,
        grace: float,
        *,
        reload: bool = False,
        scheme: str = "http",
    ) -> None:
        self.target = target
        self.listener = listener
        self.count = workers
        self.grace = grace + KILL_MARGIN
        self.reload = reload
        self.scheme = scheme
        self.processes: list[subprocess.Popen[bytes]] = []
        self.stop = threading.Event()
        self.signals = 0

    def command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "hinoki.runtime.kiso",
            self.target,
            "--fd",
            str(self.listener.fileno()),
            "--parent",
            str(os.getpid()),
            *(["--dev"] if self.reload else []),
        ]

    def spawn(self) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - our own interpreter and module
            self.command(), pass_fds=(self.listener.fileno(),)
        )

    def on_signal(self, _number: int, _frame: FrameType | None) -> None:
        """First signal: stop gracefully. Another one: kill the workers now."""
        self.signals += 1
        self.stop.set()
        if self.signals > 1:
            self.terminate(signal.SIGKILL)

    def terminate(self, number: signal.Signals) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(number)

    def wait_all(self) -> None:
        """Wait for the workers up to the grace period, then kill the rest."""
        deadline = time.monotonic() + self.grace
        for process in self.processes:
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def stop_workers(self) -> None:
        self.terminate(signal.SIGTERM)
        self.wait_all()
        self.processes = []

    def ticks(self) -> Iterator[set[tuple[Any, str]]]:
        while not self.stop.wait(POLL):
            yield set()

    def changes(self) -> Iterator[set[tuple[Any, str]]]:
        """File changes, or an empty set every so often (to look at the workers)."""
        from watchfiles import PythonFilter, watch  # noqa: PLC0415 - optional

        yield from watch(
            Path.cwd(),
            watch_filter=PythonFilter(extra_extensions=(".toml", ".env")),
            stop_event=self.stop,
            yield_on_timeout=True,
            rust_timeout=int(POLL * 1000),
            raise_interrupt=False,
        )

    def failed(self) -> int | None:
        """The exit status of a worker that ended by itself, if one did."""
        for process in self.processes:
            code = process.poll()
            if code is not None:
                return code if code > 0 else 1
        return None

    def run(self) -> int:
        for number in SIGNALS:
            signal.signal(number, self.on_signal)
        host, port = self.listener.getsockname()[:2]
        shown = f"[{host}]" if ":" in str(host) else host
        mode = "reloading" if self.reload else f"{self.count} workers"
        print(f"listening on {self.scheme}://{shown}:{port} ({mode})", flush=True)
        events = self.changes() if self.reload else self.ticks()
        # Under ``dev`` the watcher exists before the worker does, so an edit
        # made while the worker starts is not missed.
        next(events, None)  # ends at once when a signal has already come
        if self.stop.is_set():
            self.listener.close()
            return 0
        self.processes = [self.spawn() for _ in range(self.count)]
        if not self.reload:
            # No worker is ever started again: only the workers listen, so a
            # client that arrives while they stop is refused, not left waiting.
            self.listener.close()
        try:
            code = self.watch(events) if self.reload else self.supervise(events)
        finally:
            self.listener.close()  # (``dev``) no new clients while stopping
            self.stop_workers()
            # A further signal while the interpreter winds down would find the
            # default action and end the process by that signal.
            for number in SIGNALS:
                signal.signal(number, signal.SIG_IGN)
        return code

    def supervise(self, events: Iterator[set[tuple[Any, str]]]) -> int:
        for _ in events:
            code = self.failed()
            if code is not None:
                print(
                    f"error: a worker exited ({code}); stopping the others",
                    file=sys.stderr,
                    flush=True,
                )
                return code
        return 0

    def watch(self, events: Iterator[set[tuple[Any, str]]]) -> int:
        """``dev``: restart the worker when a file changes; when it exits by
        itself (an error in the code), wait for the next change."""
        waiting = False
        for changed in events:
            if changed:
                names = ", ".join(sorted({Path(name).name for _, name in changed}))
                print(f"reloading: {names} changed", flush=True)
                self.stop_workers()
                self.processes = [self.spawn()]
                waiting = False
            elif not waiting and self.failed() is not None:
                print("worker exited; waiting for changes", flush=True)
                waiting = True
        return 0
