"""Run one project command with the app's worker resource lifecycle."""

from __future__ import annotations

import inspect
from argparse import Namespace

from hinoki.app import Hinoki
from hinoki.errors import ShutdownError
from hinoki.management import Command, CommandContext, CommandError
from hinoki.modules import Module, ModuleContext, order_modules
from hinoki.runtime.executor import SyncExecutor
from hinoki.runtime.lifespan import Lifespan

_MAX_EXIT_CODE = 255


def modules_for_command(app: Hinoki, command: Command) -> tuple[Module, ...]:
    """Return the installed modules required by this command, in app order."""
    installed = {type(module): module for module in app.modules}
    required: set[type[Module]] = set()
    pending = list(command.requires)
    while pending:
        module_type = pending.pop()
        if module_type in required:
            continue
        module = installed.get(module_type)
        if module is None:
            raise CommandError(
                f"command {command.name!r} requires {module_type.__name__}, "
                "which is not installed on this app"
            )
        required.add(module_type)
        pending.extend(type(module).requires)
    selected = tuple(module for module in app.modules if type(module) in required)
    return tuple(order_modules(selected))


async def run_command(app: Hinoki, command: Command, args: Namespace) -> int:
    """Run a command and close its declared resources before returning."""
    modules = modules_for_command(app, command)
    module_context = ModuleContext(app.settings)
    executor = SyncExecutor(app.runtime.sync_workers, app.runtime.sync_queue)
    lifespan = Lifespan(modules, module_context, executor, app.runtime)
    await lifespan.startup()
    context = CommandContext(app.settings, module_context)
    try:
        result = await executor.run(command.handle, context, args)
        if inspect.isawaitable(result):
            result = await result
    except BaseException as command_error:
        try:
            await lifespan.shutdown()
        except ShutdownError as shutdown_error:
            command_error.add_note(str(shutdown_error))
        raise
    else:
        await lifespan.shutdown()
    if result is None:
        return 0
    if type(result) is not int or not 0 <= result <= _MAX_EXIT_CODE:
        raise CommandError(
            "a management command must return None or an exit code from 0 to 255"
        )
    return result
