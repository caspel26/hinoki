"""``hinoki admin create-superuser|set-password``: the admin's own users from a shell.

The first superuser is made here; every other user is created from the admin
itself. Passwords are read without echo from a terminal, or one line from stdin with
``--password-stdin``: never from the command line, which other users of the machine
can read in the process list.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from argparse import ArgumentParser, Namespace, _SubParsersAction

from hinoki.admin.users import AdminAccounts, AdminUser, AdminUserError
from hinoki.db import Database, DatabaseError
from hinoki.orm import use_database
from hinoki.project import ProjectError, find_manifest_or_none, load_manifest
from hinoki.projectmigrations import URL_VARIABLE, migration_database_url

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
            default=os.environ.get(URL_VARIABLE),
            help=f"connection URL; prefer {URL_VARIABLE}, which keeps a password "
            "out of the process list",
        )


def read_password(args: Namespace) -> str:
    """The password from stdin or a no-echo prompt (twice, to catch a typo)."""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
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


async def run_action(args: Namespace, url: str) -> str:
    database = await Database.connect(url, max_size=2)
    try:
        with use_database(database):
            return await ACTIONS[args.admin_action](AdminAccounts(), args)
    finally:
        database.close()


def run(args: Namespace) -> int:
    """Run one admin command with user-friendly errors."""
    try:
        path = find_manifest_or_none()
        url = migration_database_url(args, load_manifest(path) if path else None)
        message = asyncio.run(run_action(args, url))
    except (AdminUserError, ProjectError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except DatabaseError:
        print(f"error: {APPLY_HINT}", file=sys.stderr)
        return 1
    print(message)
    return 0


__all__ = ["add_arguments", "run"]
