"""``hinoki admin create-superuser|set-password``: the admin's own users from a shell.

The first superuser is made here; every other user is created from the admin
itself. Passwords are read without echo from a terminal, or one line from stdin with
``--password-stdin``: never from the command line, which other users of the machine
can read in the process list.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from argparse import ArgumentParser, Namespace, _SubParsersAction

from hinoki.admin.users import AdminAccounts, AdminUser, AdminUserError
from hinoki.cli.migrations import (
    URL_VARIABLE,
    MigrationDatabase,
    application_admin_settings,
    migration_database,
)
from hinoki.db import Database, DatabaseError
from hinoki.errors import MigrationError
from hinoki.orm import use_database
from hinoki.project import ProjectError, find_manifest_or_none, load_manifest
from hinoki.settings import AdminSettings

MAX_LINE = 1100  # characters read as a password; more is refused, not buffered
APPLY_HINT = (
    "the admin tables are missing or unreadable; register and apply them with "
    "`hinoki app add admin --models hinoki.admin.users.models --migrations "
    "hinoki.admin.users.migrations` and `hinoki migrations apply admin`"
)


def add_arguments(commands: _SubParsersAction[ArgumentParser]) -> None:
    """Register the ``admin`` command group."""
    group = commands.add_parser("admin", help="manage the admin's own users")
    actions = group.add_subparsers(dest="admin_action", required=True)
    for name, text in (
        ("create-superuser", "create a user that holds every permission"),
        ("set-password", "change a user's password and end their sessions"),
    ):
        sub = actions.add_parser(name, help=text)
        sub.add_argument("username")
        sub.add_argument(
            "--password-stdin",
            action="store_true",
            help="read the password from one line of standard input",
        )
        sub.add_argument(
            "--database",
            help=f"connection URL; prefer {URL_VARIABLE} or the app's database "
            "setting, which keep a password out of the process list",
        )


def read_password(args: Namespace) -> str:
    """The password from stdin or a no-echo prompt (twice, to catch a typo)."""
    if args.password_stdin and not sys.stdin.isatty():
        line = sys.stdin.readline(MAX_LINE)
        if len(line) >= MAX_LINE and not line.endswith("\n"):
            raise AdminUserError("The password is too long.")
        return line.rstrip("\r\n")
    if not sys.stdin.isatty():
        raise AdminUserError("There is no terminal to ask on; use --password-stdin.")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Password (again): "):
        raise AdminUserError("The two passwords differ.")
    return first


async def create_superuser(accounts: AdminAccounts, args: Namespace) -> str:
    user = await accounts.create_user(
        args.username, read_password(args), superuser=True
    )
    return f"created superuser {user.username}"


async def set_password(accounts: AdminAccounts, args: Namespace) -> str:
    user = await AdminUser.aget_or_none(username=args.username.strip().lower())
    if user is None:
        raise AdminUserError(f"There is no user named {args.username!r}.")
    await accounts.set_password(user, read_password(args))
    return f"changed the password of {user.username} and ended their sessions"


ACTIONS = {"create-superuser": create_superuser, "set-password": set_password}


async def run_action(
    args: Namespace,
    target: MigrationDatabase,
    settings: AdminSettings | None = None,
) -> str:
    try:
        database = await Database.connect(target.url, max_size=2)
    except DatabaseError:
        raise CannotConnectError(target.display()) from None
    try:
        with use_database(database):
            return await ACTIONS[args.admin_action](AdminAccounts(settings), args)
    finally:
        database.close()


class CannotConnectError(Exception):
    """The database could not be reached or refused the credentials."""


def explain(error: BaseException) -> tuple[str, int]:
    """The message and exit status for an error the command reports."""
    if isinstance(error, CannotConnectError):
        return f"cannot reach the database {error}", 1
    if isinstance(error, DatabaseError):
        return APPLY_HINT, 1
    if isinstance(error, EOFError | KeyboardInterrupt):
        return "cancelled", 2
    stops = isinstance(error, MigrationError)
    return str(error), 1 if stops else 2


def run(args: Namespace) -> int:
    """Run one admin command with user-friendly errors."""
    try:
        path = find_manifest_or_none()
        manifest = load_manifest(path) if path else None
        target = migration_database(args, manifest)
        print(target.describe(), file=sys.stderr)
        settings = application_admin_settings(manifest) if manifest else None
        message = asyncio.run(run_action(args, target, settings))
    except (
        AdminUserError,
        ProjectError,
        MigrationError,
        CannotConnectError,
        DatabaseError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        text, code = explain(error)
        print(f"error: {text}", file=sys.stderr)
        return code
    print(message)
    return 0


__all__ = ["add_arguments", "run"]
