"""Tests for the interactive shell runtime and CLI boundary."""

from __future__ import annotations

import ast
import asyncio
import codeop
import io
import threading
import time
from argparse import Namespace
from collections.abc import AsyncIterator, Coroutine
from concurrent.futures import CancelledError, Future
from contextlib import asynccontextmanager
from pathlib import Path
from types import CodeType
from typing import Any, cast

import pytest

from hinoki import Hinoki, shellcli
from hinoki.__main__ import parser
from hinoki.db import Database, DatabaseModule
from hinoki.errors import ShutdownError, StartupError
from hinoki.modules import Module, ModuleContext
from hinoki.orm import Model, database_var, field
from hinoki.project import ProjectError
from hinoki.runtime.config import RuntimeConfig
from hinoki.shell import ShellRuntime, run_console, terminal_reader
from hinoki.shellcli import _target_and_models, model_namespace, run
from hinoki.shellui import farewell, greeting, prompts
from hinoki.terminal import ANSI, Style


class ShellNote(Model, table="shell_notes"):
    id: int = field(primary_key=True)
    title: str


def _compile(source: str) -> CodeType:
    compiler = codeop.CommandCompiler()
    compiler.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    compiled = compiler(source, "<test-shell>", "single")
    assert compiled is not None
    return compiled


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_shell_uses_one_loop_for_top_level_await_and_keeps_namespace() -> None:
    runtime = ShellRuntime(Hinoki())
    namespace: dict[str, object] = {"asyncio": asyncio}
    runtime.start(namespace)
    try:
        runtime.run_cell(_compile("value = 40"))
        runtime.run_cell(_compile("value += await asyncio.sleep(0, result=2)"))
        assert namespace["value"] == 42
    finally:
        runtime.close()


def test_console_supports_multiline_and_continues_after_errors() -> None:
    runtime = ShellRuntime(Hinoki())
    namespace: dict[str, object] = {}
    runtime.start(namespace)
    lines = iter(
        [
            "def add(a, b):",
            "    return a + b",
            "",
            "if True print('bad')",
            "raise ValueError('bad')",
            "raise KeyboardInterrupt",
            "seen = add(2, 3)",
        ]
    )
    output = io.StringIO()

    def read(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    try:
        run_console(runtime, read=read, output=output)
        assert namespace["seen"] == 5
        assert "SyntaxError" in output.getvalue()
        assert "ValueError: bad" in output.getvalue()
        assert "KeyboardInterrupt" in output.getvalue()
    finally:
        runtime.close()


def test_shell_starts_modules_but_skips_application_hooks() -> None:
    events: list[str] = []

    class TestModule(Module):
        name = "test-shell-module"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            events.append("module-start")
            yield
            events.append("module-stop")

    async def startup(_ctx: ModuleContext) -> None:
        events.append("app-startup")

    async def shutdown(_ctx: ModuleContext) -> None:
        events.append("app-shutdown")

    app = Hinoki(modules=[TestModule()], on_startup=[startup], on_shutdown=[shutdown])
    runtime = ShellRuntime(app)
    runtime.start({"app": app})
    runtime.close()

    assert events == ["module-start", "module-stop"]


def test_manifest_supplies_target_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "hinoki.toml").write_text(
        '[project]\napplication = "sample.main:app"\n\n'
        '[[apps]]\nname = "sample"\nmodels = "sample.models"\n'
        'migrations = "sample.migrations"\nrequires = []\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    target, modules = _target_and_models(None, None)

    assert target == "sample.main:app"
    assert modules == ("sample.models",)


def test_target_requires_explicit_app_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _target_and_models("sample.main:app", ["sample.models"]) == (
        "sample.main:app",
        ("sample.models",),
    )
    with pytest.raises(ProjectError, match="pass TARGET"):
        _target_and_models(None, None)


def test_shell_cli_accepts_target_and_repeated_model_options() -> None:
    args = parser().parse_args(
        [
            "shell",
            "sample.main:app",
            "--models",
            "sample.models",
            "--models",
            "sample.more_models",
        ]
    )

    assert args.group == "shell"
    assert args.target == "sample.main:app"
    assert args.models == ["sample.models", "sample.more_models"]


def test_model_namespace_rejects_conflicting_class_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "shell_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module in ("first", "second"):
        (package / f"{module}.py").write_text(
            "from hinoki.orm import Model\n\n"
            "from hinoki.orm import field\n\n"
            f'class Entry(Model, table="{module}_entry"):\n'
            "    id: int = field(primary_key=True)\n",
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="occurs in both"):
        model_namespace(("shell_fixture.first", "shell_fixture.second"))


def test_model_namespace_reports_import_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "broken_shell_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "broken.py").write_text("raise RuntimeError('broken import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match=r"broken_shell_fixture\.broken"):
        model_namespace(("broken_shell_fixture.broken",))


def test_model_namespace_rejects_reserved_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "reserved_shell_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "from hinoki.orm import Model, field\n\n"
        'class settings(Model, table="reserved_settings"):\n'
        "    id: int = field(primary_key=True)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="reserved"):
        model_namespace(("reserved_shell_fixture.models",))


def test_shell_rejects_non_tty_before_loading_target() -> None:
    args = Namespace(target="missing.module:app", models=None)
    stderr = io.StringIO()
    result = run(args, stdin=io.StringIO(""), stdout=io.StringIO(), stderr=stderr)

    assert result == 2
    assert "interactive terminal" in stderr.getvalue()


def test_terminal_reader_prints_prompt_and_signals_eof() -> None:
    output = io.StringIO()
    read = terminal_reader(io.StringIO("sample\r\n"), output)

    assert read(">>> ") == "sample"
    assert output.getvalue() == ">>> "
    with pytest.raises(EOFError):
        terminal_reader(io.StringIO(""), output)(">>> ")


def test_cli_eof_closes_modules_and_skips_app_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class TestModule(Module):
        name = "shell-cli-module"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            events.append("module-start")
            yield
            events.append("module-stop")

    async def startup(_ctx: ModuleContext) -> None:
        events.append("hook-start")

    app = Hinoki(modules=[TestModule()], on_startup=[startup])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shellcli, "build_app", lambda _target: app)

    output = io.StringIO()
    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(""),
        stdout=output,
        stderr=io.StringIO(),
    )

    assert result == 0
    assert events == ["module-start", "module-stop"]
    assert ">>> " in output.getvalue()


@pytest.mark.parametrize("cell", ["raise SystemExit", "raise SystemExit(3)"])
def test_cli_exit_leaves_the_shell_without_a_traceback(
    cell: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class TestModule(Module):
        name = "shell-exit-module"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            yield
            events.append("module-stop")

    app = Hinoki(modules=[TestModule()])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shellcli, "build_app", lambda _target: app)
    output, errors = io.StringIO(), io.StringIO()

    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(f"{cell}\nprint('after exit')\n"),
        stdout=output,
        stderr=errors,
    )

    assert result == 0
    assert events == ["module-stop"]
    assert "Traceback" not in output.getvalue()
    assert "after exit" not in output.getvalue()
    assert errors.getvalue() == ""


def test_cli_reports_startup_failure_before_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenModule(Module):
        name = "shell-cli-broken-startup"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            raise RuntimeError("cannot start")
            yield

    app = Hinoki(modules=[BrokenModule()])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shellcli, "build_app", lambda _target: app)
    stderr = io.StringIO()

    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(""),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 1
    assert "failed to start" in stderr.getvalue()


def test_cli_reports_target_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_load(_target: str) -> Hinoki:
        raise ValueError("app target is invalid")

    monkeypatch.setattr(shellcli, "build_app", fail_load)
    stderr = io.StringIO()

    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(""),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 2
    assert "app target is invalid" in stderr.getvalue()


def test_cli_reports_shutdown_failure_after_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenModule(Module):
        name = "shell-cli-broken-shutdown"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            yield
            raise RuntimeError("cannot stop")

    app = Hinoki(modules=[BrokenModule()])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shellcli, "build_app", lambda _target: app)
    stderr = io.StringIO()

    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(""),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 1
    assert "error during shell shutdown" in stderr.getvalue()


def test_cli_reports_unexpected_console_error_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Hinoki()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shellcli, "build_app", lambda _target: app)

    def fail_console(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("console failed")

    monkeypatch.setattr(shellcli, "run_console", fail_console)
    stderr = io.StringIO()

    result = run(
        Namespace(target="sample.main:app", models=None),
        stdin=_Terminal(""),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert result == 1
    assert "console failed" in stderr.getvalue()


def test_console_ctrl_c_at_prompt_closes_normally() -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({})
    output = io.StringIO()

    def interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    try:
        run_console(runtime, read=interrupt, output=output)
    finally:
        runtime.close()

    assert "^C" in output.getvalue()


def test_compiled_await_uses_database_context() -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({"asyncio": asyncio})
    observed: list[Database | None] = []

    async def capture() -> None:
        observed.append(database_var.get())

    runtime.namespace["capture"] = capture
    try:
        runtime.run_cell(_compile("await capture()"))
    finally:
        runtime.close()

    assert observed == [None]


def test_shell_startup_failure_stops_its_loop() -> None:
    class BrokenModule(Module):
        name = "broken-shell-startup"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            raise RuntimeError("startup failed")
            yield

    runtime = ShellRuntime(Hinoki(modules=[BrokenModule()]))

    with pytest.raises(StartupError, match="broken-shell-startup"):
        runtime.start({})

    assert not runtime.thread.is_alive()
    runtime.close()


def test_module_generator_exit_during_startup_closes_prior_modules() -> None:
    events: list[str] = []

    class EnteredModule(Module):
        name = "entered-before-generator-exit"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            events.append("entered")
            yield
            events.append("closed")

    class GeneratorExitModule(Module):
        name = "generator-exit-during-enter"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            raise GeneratorExit
            yield

    runtime = ShellRuntime(Hinoki(modules=[EnteredModule(), GeneratorExitModule()]))

    with pytest.raises(StartupError, match="generator-exit-during-enter"):
        runtime.start({})

    assert events == ["entered", "closed"]
    assert not runtime.thread.is_alive()


def test_shell_bounds_startup_that_suppresses_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = RuntimeConfig(startup_timeout=0.02, shutdown_timeout=0.02)

    class ResistantModule(Module):
        name = "resistant-shell-startup"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue
            yield

    runtime = ShellRuntime(Hinoki(modules=[ResistantModule()], runtime=config))
    started = time.monotonic()

    with pytest.raises(StartupError, match="TimeoutError"):
        runtime.start({})

    assert time.monotonic() - started < 1
    # start() only waits a bounded time for the loop thread; on a slow host the
    # thread may still be abandoning the task, so wait for it to finish.
    runtime.thread.join(timeout=10)
    assert not runtime.thread.is_alive()
    assert "abandoning" in caplog.text


@pytest.mark.parametrize("mode", ["timeout", "error"])
def test_shell_startup_reports_when_its_loop_does_not_stop(
    mode: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = ShellRuntime(
        Hinoki(runtime=RuntimeConfig(startup_timeout=0.01, shutdown_timeout=0.01))
    )

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        if mode == "error":
            future.set_exception(
                StartupError("<lifespan>", RuntimeError("startup failed"))
            )
        return future

    monkeypatch.setattr(runtime, "_submit", submit)
    monkeypatch.setattr(runtime, "_stop_loop", lambda: False)
    with (
        caplog.at_level("ERROR", logger="hinoki.shell"),
        pytest.raises(StartupError) as raised,
    ):
        runtime.start({})
    if mode == "timeout":
        notes = " ".join(getattr(raised.value.cause, "__notes__", []))
        assert "did not stop after cancellation" in notes
    else:
        assert "did not stop after startup failure" in caplog.text
    monkeypatch.undo()
    runtime.close()


async def test_shell_preserves_cancelled_error_from_a_cell() -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({"asyncio": asyncio})
    try:
        with pytest.raises(CancelledError):
            runtime.run_cell(_compile("raise asyncio.CancelledError"))
    finally:
        runtime.close()


@pytest.mark.parametrize("mode", ["timeout", "error"])
def test_shell_close_collects_timeout_and_unexpected_failures(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ShellRuntime(Hinoki(runtime=RuntimeConfig(shutdown_timeout=0.01)))
    runtime.start({})

    def submit(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        if mode == "error":
            future.set_exception(RuntimeError("unexpected close error"))
        return future

    monkeypatch.setattr(runtime, "_submit", submit)
    with pytest.raises(ShutdownError) as raised:
        runtime.close()
    if mode == "timeout":
        assert any(isinstance(cause, TimeoutError) for cause in raised.value.causes)
    else:
        assert any(
            str(cause) == "unexpected close error" for cause in raised.value.causes
        )


def test_shell_logs_async_generator_shutdown_errors(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({})

    async def broken_shutdown() -> None:
        raise RuntimeError("async generator close failed")

    monkeypatch.setattr(runtime.loop, "shutdown_asyncgens", broken_shutdown)
    with caplog.at_level("ERROR", logger="hinoki.shell"):
        runtime.close()
    assert "error closing shell async generators" in caplog.text


def test_shell_bounds_async_generator_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def slow_generator() -> AsyncIterator[int]:
        try:
            yield 1
        finally:
            await asyncio.Event().wait()

    runtime = ShellRuntime(Hinoki(runtime=RuntimeConfig(shutdown_timeout=0.01)))
    runtime.start({"generator": slow_generator()})
    runtime.run_cell(_compile("await generator.__anext__()"))
    with caplog.at_level("WARNING", logger="hinoki.shell"):
        runtime.close()
    assert "async generator cleanup exceeded" in caplog.text


def test_shell_reports_module_shutdown_failure_and_stops_loop() -> None:
    class BrokenModule(Module):
        name = "broken-shell-shutdown"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            yield
            raise RuntimeError("shutdown failed")

    runtime = ShellRuntime(Hinoki(modules=[BrokenModule()]))
    runtime.start({})

    with pytest.raises(ShutdownError, match="RuntimeError"):
        runtime.close()

    assert not runtime.thread.is_alive()
    runtime.close()


def test_shell_cancels_user_tasks_when_the_session_closes() -> None:
    runtime = ShellRuntime(Hinoki())
    stopped = threading.Event()

    async def wait_forever() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runtime.start({"asyncio": asyncio, "wait_forever": wait_forever})
    runtime.run_cell(_compile("asyncio.create_task(wait_forever())"))
    runtime.close()

    assert stopped.wait(timeout=1)


def test_shell_bounds_shutdown_when_a_task_ignores_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = RuntimeConfig(shutdown_timeout=0.02)
    runtime = ShellRuntime(Hinoki(runtime=config))
    cancelled = threading.Event()

    async def resistant_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.Event().wait()

    runtime.start({"asyncio": asyncio, "resistant_task": resistant_task})
    runtime.run_cell(_compile("asyncio.create_task(resistant_task())"))
    runtime.run_cell(_compile("await asyncio.sleep(0)"))
    started = time.monotonic()
    runtime.close()

    assert cancelled.is_set()
    assert time.monotonic() - started < 1
    assert "abandoning 1 shell task" in caplog.text


def test_shell_waits_for_each_module_cleanup_within_its_timeout() -> None:
    config = RuntimeConfig(shutdown_timeout=0.05)
    closed: list[str] = []

    class FirstModule(Module):
        name = "first-shell-cleanup"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            yield
            await asyncio.sleep(0.03)
            closed.append(self.name)

    class SecondModule(Module):
        name = "second-shell-cleanup"

        @asynccontextmanager
        async def lifespan(self, _ctx: ModuleContext) -> AsyncIterator[None]:
            yield
            await asyncio.sleep(0.03)
            closed.append(self.name)

    runtime = ShellRuntime(
        Hinoki(modules=[FirstModule(), SecondModule()], runtime=config)
    )
    runtime.start({})

    runtime.close()

    assert closed == ["second-shell-cleanup", "first-shell-cleanup"]


def test_shell_orm_query_uses_installed_sqlite_module(tmp_path: Path) -> None:
    app = Hinoki(modules=[DatabaseModule(f"sqlite://{tmp_path / 'shell.sqlite3'}")])
    runtime = ShellRuntime(app)
    namespace: dict[str, object] = {"ShellNote": ShellNote}
    runtime.start(namespace)

    async def seed() -> None:
        database = runtime.context.get(Database)
        await database.execute(
            "CREATE TABLE shell_notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
        )
        await database.execute("INSERT INTO shell_notes (title) VALUES (?)", "sample")

    try:
        runtime._submit(seed()).result()
        runtime.run_cell(_compile("rows = await ShellNote.all()"))
        rows = namespace["rows"]
        assert isinstance(rows, list)
        assert [row.title for row in rows] == ["sample"]
    finally:
        runtime.close()


def test_run_cell_interrupt_cancels_current_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({})

    def interrupted(coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        coroutine.close()
        future: Future[Any] = Future()
        future.set_exception(KeyboardInterrupt())
        return future

    monkeypatch.setattr(runtime, "_submit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runtime.run_cell(_compile("pass"))
    monkeypatch.undo()
    runtime.close()


def test_shutdown_reports_loop_thread_that_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ShellRuntime(Hinoki())
    runtime.start({})
    stop_loop = runtime._stop_loop
    monkeypatch.setattr(runtime, "_stop_loop", lambda: False)

    with pytest.raises(ShutdownError, match="TimeoutError"):
        runtime.close()

    monkeypatch.setattr(runtime, "_stop_loop", stop_loop)
    runtime.close()
    assert not runtime.thread.is_alive()


def test_loop_closes_even_when_pending_task_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = ShellRuntime(Hinoki())

    def fail_cleanup() -> None:
        raise RuntimeError("loop cleanup failed")

    monkeypatch.setattr(runtime, "_cancel_pending_tasks", fail_cleanup)
    runtime.start({})
    runtime.close()

    assert not runtime.thread.is_alive()
    assert "unexpected error while closing" in caplog.text


@pytest.mark.parametrize(
    ("error", "output"),
    [
        (KeyboardInterrupt(), "KeyboardInterrupt"),
        (RuntimeError("runtime broke"), "RuntimeError: runtime broke"),
    ],
)
def test_console_keeps_running_after_control_flow_exceptions(
    error: BaseException, output: str
) -> None:
    class FailingRuntime:
        def run_cell(self, _code: CodeType) -> None:
            raise error

    lines = iter(["pass"])

    def read(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    terminal = io.StringIO()

    run_console(cast(ShellRuntime, FailingRuntime()), read=read, output=terminal)

    assert output in terminal.getvalue()


FANCY = Style(color=True, unicode=True)


def test_plain_shell_keeps_python_prompts_and_the_original_greeting() -> None:
    plain = Style()
    assert prompts(plain) == (">>> ", "... ")
    assert greeting(plain, "App", "Settings", ["Note"]) == [
        "Hinoki interactive shell; use Ctrl-D or Ctrl-C to exit."
    ]
    assert farewell(plain) == ""


def test_branded_shell_greets_with_app_models_and_branded_prompts() -> None:
    names = [f"Model{index}" for index in range(9)]
    text = "\n".join(greeting(FANCY, "Blog", "BlogSettings", names))

    assert "ctrl-d to exit" in text and "Blog" in text and "BlogSettings" in text
    assert "hinoki \u203a shell" in ANSI.sub("", text)
    assert "Model0" in text and "+3 more" in text and "Model8" not in text
    assert "await" in text
    primary, continuation = prompts(FANCY)
    assert "hinoki" in primary and "\u276f" in primary
    assert "\u00b7" in continuation
    assert "\x1b[" in primary


def test_branded_shell_greets_an_application_without_models() -> None:
    text = ANSI.sub("", "\n".join(greeting(FANCY, "Blog", "BlogSettings", [])))

    assert "models" in text and "none" in text


def test_run_console_uses_the_prompts_it_is_given(tmp_path: Path) -> None:
    app = Hinoki()
    runtime = ShellRuntime(app)
    runtime.start({})
    asked: list[str] = []
    lines = iter(["if True:", "    pass", ""])

    def read(prompt: str) -> str:
        asked.append(prompt)
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    try:
        run_console(runtime, read=read, output=io.StringIO(), prompts=("P> ", "C> "))
    finally:
        runtime.close()
    assert asked[:2] == ["P> ", "C> "]
