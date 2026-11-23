"""Persistent-loop runtime and top-level-await console for ``hinoki shell``."""

from __future__ import annotations

import ast
import asyncio
import codeop
import functools
import inspect
import logging
import threading
import traceback
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from types import CodeType
from typing import Any, TextIO

from hinoki.app import Hinoki
from hinoki.errors import ShutdownError, StartupError
from hinoki.mail import Mailer, mailer_var
from hinoki.modules import ModuleContext
from hinoki.orm import database_var
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.lifespan import Lifespan

log = logging.getLogger("hinoki.shell")


class _EvaluationError(Exception):
    """Carry user-raised BaseExceptions without letting asyncio stop the loop."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(str(cause))


class ShellRuntime:
    """Keep module resources and a single event loop alive for the session."""

    def __init__(self, app: Hinoki) -> None:
        self.app = app
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._run_loop, name="hinoki-shell-loop", daemon=True
        )
        self.context = ModuleContext(app.settings)
        self.executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
        # Shell deliberately does not pass app hooks to Lifespan.
        self.lifespan = Lifespan(app.modules, self.context, self.executor, app.runtime)
        self.namespace: dict[str, object] = {}

    def start(self, namespace: dict[str, object]) -> None:
        """Start the loop and app modules, cleaning up when startup fails."""
        self.namespace = namespace
        self.thread.start()
        self.ready.wait()
        startup_timeout = (
            self.app.runtime.startup_timeout
            + (2 + len(self.lifespan.modules)) * self.app.runtime.shutdown_timeout
            + 0.1
        )
        future = self._submit(self.lifespan.startup())
        try:
            future.result(timeout=startup_timeout)
        except FutureTimeout as error:
            future.cancel()
            stopped = self._stop_loop()
            cause = TimeoutError("shell startup exceeded its deadline")
            if not stopped:
                cause.add_note("the shell event loop did not stop after cancellation")
            raise StartupError("<lifespan>", cause) from error
        except BaseException:
            if not self._stop_loop():
                log.error("shell event loop did not stop after startup failure")
            raise

    async def evaluate(self, code: CodeType) -> None:
        """Evaluate one compiled cell in the persistent shell namespace."""
        from hinoki.db import Database  # noqa: PLC0415 - avoid CLI import coupling

        token = database_var.set(self.context.find(Database))
        mailer_token = mailer_var.set(self.context.find(Mailer))
        try:
            try:
                result = eval(code, self.namespace)  # noqa: S307 - this is the Python REPL
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                raise _EvaluationError(error) from None
        finally:
            mailer_var.reset(mailer_token)
            database_var.reset(token)

    def run_cell(self, code: CodeType) -> None:
        """Run a compiled cell on the owned loop and propagate its exception."""
        future = self._submit(self.evaluate(code))
        try:
            future.result()
        except KeyboardInterrupt:
            future.cancel()
            raise

    def close(self) -> None:
        """Close module resources before stopping the event loop thread."""
        if not self.thread.is_alive():
            return
        causes: list[BaseException] = []
        timeout = (2 + len(self.lifespan.modules)) * self.app.runtime.shutdown_timeout
        try:
            self._submit(self.lifespan.shutdown()).result(timeout=timeout + 0.1)
        except FutureTimeout:
            causes.append(TimeoutError("shell resource shutdown exceeded its deadline"))
        except ShutdownError as error:
            causes.extend(error.causes)
        except BaseException as error:
            causes.append(error)
        finally:
            if not self._stop_loop():
                causes.append(
                    TimeoutError("shell event loop did not stop by its deadline")
                )
        if causes:
            raise ShutdownError(causes)

    def _submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        try:
            self._cancel_pending_tasks()
        except BaseException:
            log.exception("unexpected error while closing the shell event loop")
        finally:
            self.loop.close()

    def _cancel_pending_tasks(self) -> None:
        """Cancel user tasks and bound cleanup by the app shutdown deadline."""
        pending = asyncio.all_tasks(self.loop)
        done, pending = self._cancel_and_wait(pending)
        if pending:
            finished, pending = self._cancel_and_wait(pending)
            done.update(finished)
        if done:
            self.loop.run_until_complete(asyncio.gather(*done, return_exceptions=True))
        if pending:
            log.warning(
                "abandoning %d shell task(s) after the shutdown timeout",
                len(pending),
            )
        if not pending:
            shutdown = self.loop.create_task(self.loop.shutdown_asyncgens())
            done, pending = self.loop.run_until_complete(
                asyncio.wait({shutdown}, timeout=self.app.runtime.shutdown_timeout)
            )
            if done:
                try:
                    shutdown.result()
                except BaseException:
                    log.exception("error closing shell async generators")
            else:
                shutdown.cancel()
                log.warning("async generator cleanup exceeded the shutdown timeout")

    def _cancel_and_wait(
        self, tasks: set[asyncio.Task[Any]]
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        """Request cancellation, then wait at most one shutdown timeout."""
        if not tasks:
            return set(), set()
        for task in tasks:
            task.cancel()
        return self.loop.run_until_complete(
            asyncio.wait(tasks, timeout=self.app.runtime.shutdown_timeout)
        )

    def _stop_loop(self) -> bool:
        self.loop.call_soon_threadsafe(self.loop.stop)
        join_timeout = self.app.runtime.shutdown_timeout * 2 + 0.1
        self.thread.join(timeout=join_timeout)
        return not self.thread.is_alive()


def run_console(
    runtime: ShellRuntime,
    *,
    read: Callable[[str], str] = input,
    output: TextIO,
    prompts: tuple[str, str] = (">>> ", "... "),
) -> None:
    """Compile multiline Python cells and keep the prompt usable after errors."""
    compiler = codeop.CommandCompiler()
    compiler.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    source = ""
    while True:
        try:
            line = read(prompts[1] if source else prompts[0])
        except EOFError:
            return
        except KeyboardInterrupt:
            print("^C", file=output)
            return
        source += line + "\n"
        try:
            compiled = compiler(source, "<hinoki-shell>", "single")
        except (SyntaxError, OverflowError, ValueError):
            traceback.print_exc(file=output)
            source = ""
            continue
        if compiled is None:
            continue
        source = ""
        if _run_compiled(runtime, compiled, output):
            return


def terminal_reader(stdin: TextIO, stdout: TextIO) -> Callable[[str], str]:
    """Build an input function that prints prompts to the selected terminal."""
    return functools.partial(_read_line, stdin, stdout)


def _read_line(stdin: TextIO, stdout: TextIO, prompt: str) -> str:
    print(prompt, end="", file=stdout, flush=True)
    line = stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def _run_compiled(runtime: ShellRuntime, compiled: CodeType, output: TextIO) -> bool:
    """Run one cell; ``True`` when it asked to leave the shell (``exit()``)."""
    try:
        runtime.run_cell(compiled)
    except _EvaluationError as error:
        if isinstance(error.cause, SystemExit):
            return True
        if isinstance(error.cause, KeyboardInterrupt):
            print("KeyboardInterrupt", file=output)
        else:
            traceback.print_exception(error.cause, file=output)
    except KeyboardInterrupt:
        print("KeyboardInterrupt", file=output)
    except BaseException:
        traceback.print_exc(file=output)
    return False
