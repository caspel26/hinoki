"""``python -m hinoki migrations plan|apply|status|sql`` and ``check``."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from hinoki import migrator
from hinoki.checkcli import add_arguments, run_check
from hinoki.db import Database
from hinoki.errors import MigrationError
from hinoki.migrations import migration_files, models_of, plan

URL_VARIABLE = "HINOKI_DATABASE_URL"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m hinoki")
    commands = root.add_subparsers(dest="group", required=True)
    add_arguments(commands)
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


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.group == "check":
        return run_check(args)
    try:
        if args.action == "plan":
            run_plan(args)
        elif args.action == "sql":
            run_sql(args)
        else:
            asyncio.run(run_database(args))
    except MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
