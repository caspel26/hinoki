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
* Reload (``serve`` with ``server.sighup`` not ``"stop"``): the supervisor keeps
  the listener open, and SIGHUP replaces the workers one at a time. A new worker
  is started on the same socket and reports once its lifespan has started and it
  serves (a byte on a pipe, ``--ready-fd``; an end of file without it means it
  failed). Only then is the old worker stopped gracefully. A new worker that does
  not become ready is killed and the reload stops, the others keep running.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

from hinoki.terminal import Style

POLL = 0.25
# Windows has no SIGHUP, so a reload there is never offered (``serve`` checks).
HANGUP: int | None = getattr(signal, "SIGHUP", None)
SIGNALS = tuple(
    number for number in (signal.SIGINT, signal.SIGTERM, HANGUP) if number is not None
)
# Seconds past ``shutdown_timeout`` before a worker is killed.
KILL_MARGIN = 5.0
# Seconds past the lifespan startup timeout that a new worker has to report ready
# (importing the target comes before the lifespan).
READY_MARGIN = 5.0


def docs_line(url: str, style: Style) -> str:
    """The ``docs:`` line: the address in the brand colour on a colour terminal,
    plain text otherwise."""
    return f"{style.paint('docs:', 'brand', bold=True)} {style.paint(url, 'brand')}"


class Supervisor:
    """Owns the listener and the worker processes of one ``serve`` or ``dev`` run."""

    def __init__(  # noqa: PLR0913 - the listeners and run options
        self,
        target: str,
        listener: socket.socket,
        workers: int,
        grace: float,
        *,
        reload: bool = False,
        scheme: str = "http",
        interface: str = "asgi",
        metrics: socket.socket | None = None,
        rolling: bool = False,
        startup_timeout: float = 30.0,
        reuse_port: bool = False,
    ) -> None:
        self.target = target
        self.interface = interface
        # SIGHUP replaces the workers one by one (never under ``dev``).
        self.rolling = rolling and not reload and HANGUP is not None
        self.reuse_port = reuse_port and not reload
        self.address: tuple[str, int] | None = None
        self.ready_wait = startup_timeout + READY_MARGIN
        self.hangup = threading.Event()  # a SIGHUP waiting to be served
        self.generation = 1
        self.next_worker = workers  # the numbers 0 to workers - 1 are taken
        self.worker_of: dict[int, int] = {}  # pid -> worker number
        # The shared metrics listener, opened here like the main one, and the
        # private directory where the workers leave their snapshots. During a
        # reload two workers serve at once, so a single worker needs one too.
        self.metrics = metrics
        self.metrics_dir = (
            tempfile.mkdtemp(prefix="hinoki-metrics-")
            if metrics and (workers > 1 or self.rolling)
            else None
        )
        self.docs: str | None = None  # path of the docs page, announced on start
        self.listener = listener
        self.count = workers
        self.grace = grace + KILL_MARGIN
        self.reload = reload
        self.scheme = scheme
        self.processes: list[subprocess.Popen[bytes]] = []
        # Replaced workers that are still finishing their requests.
        self.draining: list[subprocess.Popen[bytes]] = []
        self.stop = threading.Event()
        self.signals = 0

    def listener_arguments(self) -> list[str]:
        if self.reuse_port:
            host, port = (
                self.address
                if self.address is not None
                else self.listener.getsockname()[:2]
            )
            return ["--host", str(host), "--port", str(port), "--reuse-port"]
        return ["--fd", str(self.listener.fileno())]

    def command(self, slot: int = 0, ready_fd: int | None = None) -> list[str]:
        return [
            sys.executable,
            "-m",
            "hinoki.runtime.kiso",
            self.target,
            *self.listener_arguments(),
            "--parent",
            str(os.getpid()),
            *(["--inline-dispatch"] if self.count > 1 else []),
            *(["--interface", self.interface] if self.interface != "asgi" else []),
            *(["--dev"] if self.reload else []),
            *(["--ready-fd", str(ready_fd)] if ready_fd is not None else []),
            *self.metrics_arguments(slot),
        ]

    def metrics_arguments(self, slot: int) -> list[str]:
        if self.metrics is None:
            return []
        return [
            "--metrics-fd",
            str(self.metrics.fileno()),
            *(["--metrics-dir", self.metrics_dir] if self.metrics_dir else []),
            "--slot",
            str(slot),
        ]

    def spawn(
        self, slot: int = 0, ready_fd: int | None = None
    ) -> subprocess.Popen[bytes]:
        """Start a worker with SIGINT blocked: a terminal Ctrl-C reaches the
        whole process group, and while the worker still imports Hinoki it
        would end it with a traceback. The supervisor owns the stop (SIGTERM);
        the worker unblocks SIGINT first thing, behind a no-op handler
        (``kiso.take_interrupts``)."""
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        inherited = [] if self.reuse_port else [self.listener.fileno()]
        if self.metrics is not None:
            inherited.append(self.metrics.fileno())
        if ready_fd is not None:
            inherited.append(ready_fd)
        try:
            process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
                self.command(slot, ready_fd), pass_fds=tuple(inherited)
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        self.worker_of[process.pid] = slot
        return process

    def on_signal(self, number: int, _frame: FrameType | None) -> None:
        """First signal: stop gracefully (SIGHUP reloads instead, when the
        supervisor rolls). Another one: kill the workers now."""
        if self.rolling and number == HANGUP:
            # A second one meanwhile is the same request; while stopping it is
            # ignored and never counts toward the kill.
            if not self.stop.is_set():
                self.hangup.set()
            return
        self.signals += 1
        self.stop.set()
        if self.signals > 1:
            self.terminate(signal.SIGKILL)

    def terminate(self, number: signal.Signals) -> None:
        for process in (*self.processes, *self.draining):
            if process.poll() is None:
                process.send_signal(number)

    def wait_all(self) -> None:
        """Wait for the workers up to the grace period, then kill the rest."""
        deadline = time.monotonic() + self.grace
        for process in (*self.processes, *self.draining):
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def stop_workers(self) -> None:
        self.terminate(signal.SIGTERM)
        self.wait_all()
        self.processes = []
        self.draining = []

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
        self.address = (str(host), int(port))
        shown = f"[{host}]" if ":" in str(host) else host
        mode = "reloading" if self.reload else f"{self.count} workers"
        print(f"listening on {self.scheme}://{shown}:{port} ({mode})", flush=True)
        if self.docs:
            url = f"{self.scheme}://{shown}:{port}{self.docs}"
            print(docs_line(url, Style.detect(sys.stdout)), flush=True)
        events = self.changes() if self.reload else self.ticks()
        # Under ``dev`` the watcher exists before the worker does, so an edit
        # made while the worker starts is not missed.
        next(events, None)  # ends at once when a signal has already come
        if self.stop.is_set():
            self.release()
            self.discard_snapshots()
            return 0
        if self.reuse_port:
            # Close the supervisor's probe listener before workers bind their own
            # SO_REUSEPORT sockets, so the kernel never routes connections here.
            self.listener.close()
        self.processes = [self.spawn(slot) for slot in range(self.count)]
        if not self.reload and not self.rolling:
            # No worker is ever started again: only the workers listen, so a
            # client that arrives while they stop is refused, not left waiting.
            self.release()
        try:
            code = self.watch(events) if self.reload else self.supervise(events)
        finally:
            self.release()  # (``dev``) no new clients while stopping
            self.stop_workers()
            self.discard_snapshots()
            # A further signal while the interpreter winds down would find the
            # default action and end the process by that signal.
            for number in SIGNALS:
                signal.signal(number, signal.SIG_IGN)
        return code

    def release(self) -> None:
        """Close the supervisor's own copies of the listening sockets."""
        self.listener.close()
        if self.metrics is not None:
            self.metrics.close()

    def discard_snapshots(self) -> None:
        if self.metrics_dir is not None:
            shutil.rmtree(self.metrics_dir, ignore_errors=True)

    def supervise(self, events: Iterator[set[tuple[Any, str]]]) -> int:
        for _ in events:
            if self.hangup.is_set():
                self.hangup.clear()
                self.roll()
            code = self.failed()
            if code is not None:
                print(
                    f"error: a worker exited ({code}); stopping the others",
                    file=sys.stderr,
                    flush=True,
                )
                return code
        return 0

    def roll(self) -> None:
        """Replace the workers one at a time, each only once its successor
        serves. A successor that fails ends the reload: the others keep running."""
        target = self.generation + 1
        total = len(self.processes)
        print(f"reloading: SIGHUP (generation {target})", flush=True)
        for index in range(total):
            if self.stop.is_set():
                return
            worker = self.next_worker
            self.next_worker += 1
            fresh = self.start_ready(worker)
            if fresh is None:
                if not self.stop.is_set():
                    print(
                        f"error: reload stopped at worker {index + 1} of {total}; "
                        "the other workers keep running",
                        file=sys.stderr,
                        flush=True,
                    )
                return
            old, self.processes[index] = self.processes[index], fresh
            self.generation = target
            self.retire(old)
        print(f"reloaded {total} workers (generation {target})", flush=True)

    def start_ready(self, worker: int) -> subprocess.Popen[bytes] | None:
        """Start a worker and wait until it serves; ``None`` (and it is gone)
        when it did not, or when the supervisor is stopping."""
        reader, writer = os.pipe()
        try:
            process = self.spawn(worker, ready_fd=writer)
        except OSError as error:
            os.close(reader)
            print(
                f"error: the new worker could not start ({error})",
                file=sys.stderr,
                flush=True,
            )
            return None
        finally:
            os.close(writer)
        try:
            problem = self.await_ready(reader)
        finally:
            os.close(reader)
        if problem is None:
            return process
        if self.stop.is_set():
            self.draining.append(process)  # stopped with the others, gracefully
            return None
        if process.poll() is None:
            process.kill()
        process.wait()
        print(f"error: the new worker {problem}", file=sys.stderr, flush=True)
        return None

    def await_ready(self, reader: int) -> str | None:
        """``None`` once the worker wrote its byte, else why it never did."""
        deadline = time.monotonic() + self.ready_wait
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"was not ready after {self.ready_wait:g} seconds"
            if select.select([reader], [], [], min(POLL, remaining))[0]:
                if os.read(reader, 1):
                    return None
                return "exited before it was ready"
        return "was interrupted"

    def retire(self, old: subprocess.Popen[bytes]) -> None:
        """Stop a replaced worker gracefully, killing it past the grace period."""
        self.draining.append(old)
        if old.poll() is None:
            old.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + self.grace
        while old.poll() is None and not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                old.kill()
                break
            try:
                old.wait(min(POLL, remaining))
            except subprocess.TimeoutExpired:
                continue
        if self.stop.is_set():
            return  # ``stop_workers`` waits for it with the rest
        old.wait()
        self.draining.remove(old)
        self.discard_snapshot(self.worker_of.pop(old.pid, None))

    def discard_snapshot(self, worker: int | None) -> None:
        """Remove the snapshot a replaced worker left, so its numbers leave the
        scrapes at once."""
        if self.metrics_dir is None or worker is None:
            return
        for name in (f"worker-{worker}.prom", f"worker-{worker}.tmp"):
            Path(self.metrics_dir, name).unlink(missing_ok=True)

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
