"""``hinoki migrations plan|apply|status|sql`` and ``check``."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from hinoki import serve, settingscli
from hinoki.checkcli import add_arguments, run_check
from hinoki.db import Database
from hinoki.errors import MigrationError
from hinoki.orm import migrator
from hinoki.orm.migrations import migration_files, models_of, plan

URL_VARIABLE = "HINOKI_DATABASE_URL"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hinoki")
    commands = root.add_subparsers(dest="group", required=True)
    add_arguments(commands)
    settingscli.add_arguments(commands)
    serve.add_arguments(commands)
    commands.add_parser("manage", help="run an app's management commands")
    group = commands.add_parser("migrations", help="plan, apply and inspect migrations")
    group.add_argument("--dir", default="migrations", help="migrations directory")
    actions = group.add_subparsers(dest="action", required=True)
    planner = actions.add_parser("plan", help="write a migration from the models")
    planner.add_argument("--models", required=True, help="module that defines models")
    planner.add_argument("--name", default="changes", help="migration name")
    planner.add_argument("--allow-destructive", action="store_true")
    planner.add_argument("--dry-run", action="store_true", help="print, write nothing")
    for name, text in (("apply", "apply pending migrations"), ("status", "list them")):
        sub = actions.add_parser(name, help=text)
        sub.add_argument("--database", default=os.environ.get(URL_VARIABLE))
    show = actions.add_parser("sql", help="print one migration's SQL")
    show.add_argument("name")
    return root


def run_plan(args: argparse.Namespace) -> None:
    result = plan(
        models_of(args.models),
        Path(args.dir),
        args.name,
        allow_destructive=args.allow_destructive,
        write=not args.dry_run,
    )
    if not result.operations:
        print("no changes")
    for operation in result.operations:
        print(
            f"{'DESTRUCTIVE ' if operation.destructive else ''}{operation.description}"
        )
    if result.path:
        print(f"wrote {result.path}")


async def run_database(args: argparse.Namespace) -> None:
    if not args.database:
        raise MigrationError(f"pass --database or set {URL_VARIABLE}")
    database = await Database.connect(args.database, max_size=1)
    try:
        if args.action == "apply":
            applied = await migrator.apply(database, Path(args.dir))
            print("\n".join(applied) or "nothing to apply")
        else:
            for name, state in await migrator.status(database, Path(args.dir)):
                print(f"{state:8} {name}")
    finally:
        database.close()


def run_sql(args: argparse.Namespace) -> None:
    for path in migration_files(Path(args.dir)):
        if path.stem == args.name:
            print(path.read_text(encoding="utf-8"), end="")
            return
    raise MigrationError(f"no migration named {args.name!r}")


def run_new(args: argparse.Namespace) -> int:
    from hinoki.scaffold import ScaffoldError, create  # noqa: PLC0415 - only for `new`

    try:
        target = create(args.name, Path(args.dir))
    except ScaffoldError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"created {target}; next: cd {target} && uv sync && uv run pytest")
    if "tool.uv.sources" not in (target / "pyproject.toml").read_text("utf-8"):
        print(
            "note: Hinoki was not run from a source checkout, so pyproject.toml "
            'names plain "hinoki" (an unrelated package on PyPI). Point it at your '
            "Hinoki source with [tool.uv.sources] before running uv sync.",
            file=sys.stderr,
        )
    return 0


def run_mcp_command(args: argparse.Namespace) -> int:
    from hinoki.mcp import run_mcp  # noqa: PLC0415 - loaded only for `mcp`

    return run_mcp(args)


def run_migrations(args: argparse.Namespace) -> int:
    try:
        if args.action == "plan":
            run_plan(args)
        elif args.action == "sql":
            run_sql(args)
        else:
            asyncio.run(run_database(args))
    except MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "  fix: correct the model or migration state; inspect with "
            "`hinoki migrations plan`",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "manage":
        from hinoki.managementcli import run  # noqa: PLC0415 - only for `manage`

        return run(arguments[1:])
    args = parser().parse_args(arguments)
    commands = {
        "check": run_check,
        "new": run_new,
        "mcp": run_mcp_command,
        "migrations": run_migrations,
        "settings": settingscli.run_settings,
        "dev": serve.run_dev,
        "serve": serve.run_serve,
    }
    return commands[args.group](args)


if __name__ == "__main__":
    sys.exit(main())
