"""``hinoki migrations plan|apply|status|sql`` and ``check``."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from hinoki import (
    doctorcli,
    openapicli,
    projectcli,
    routescli,
    serve,
    settingscli,
    shellcli,
    versioncli,
)
from hinoki.checkcli import add_arguments, run_check
from hinoki.db import Database, DatabaseError
from hinoki.errors import MigrationError
from hinoki.orm import migrator
from hinoki.orm.migrations import Plan, migration_files, models_of, plan
from hinoki.project import ProjectError
from hinoki.projectmigrations import (
    manifest_for,
    plan_project,
    run_database_project,
    sql_project,
)
from hinoki.settings import DatabaseSettings

URL_VARIABLE = "HINOKI_DATABASE_URL"


def database_dialect(url: str) -> str:
    """Return the migration dialect selected by a database URL scheme."""
    for scheme, dialect in (
        ("sqlite://", "sqlite"),
        ("mysql://", "mysql"),
        ("mariadb://", "mariadb"),
        ("postgres://", "postgres"),
        ("postgresql://", "postgres"),
    ):
        if url.startswith(scheme):
            return dialect
    raise MigrationError("database URL must use a supported backend scheme")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hinoki")
    commands = root.add_subparsers(dest="group", required=True)
    add_arguments(commands)
    settingscli.add_arguments(commands)
    routescli.add_arguments(commands)
    openapicli.add_arguments(commands)
    versioncli.add_arguments(commands)
    projectcli.add_arguments(commands)
    doctorcli.add_arguments(commands)
    shellcli.add_arguments(commands)
    serve.add_arguments(commands)
    commands.add_parser("manage", help="run an app's management commands")
    group = commands.add_parser("migrations", help="plan, apply and inspect migrations")
    group.add_argument(
        "--dir", help="legacy migrations directory (without hinoki.toml)"
    )
    actions = group.add_subparsers(dest="action", required=True)
    planner = actions.add_parser("plan", help="write a migration from the models")
    planner.add_argument("--models", help="legacy module that defines models")
    planner.add_argument("--app", help="project app to plan")
    planner.add_argument(
        "--name",
        help="optional migration slug (default: derived from the planned change)",
    )
    planner.add_argument("--allow-destructive", action="store_true")
    planner.add_argument("--dry-run", action="store_true", help="print, write nothing")
    planner.add_argument(
        "--check",
        action="store_true",
        help="fail if model changes are missing from migration files",
    )
    planner.add_argument(
        "--database",
        default=os.environ.get(URL_VARIABLE),
        help=f"target database URL; defaults to local SQLite or {URL_VARIABLE}",
    )
    for name, text in (("apply", "apply pending migrations"), ("status", "list them")):
        sub = actions.add_parser(name, help=text)
        sub.add_argument(
            "--database",
            default=os.environ.get(URL_VARIABLE),
            help=f"connection URL; prefer {URL_VARIABLE}, which keeps a password "
            "out of the process list",
        )
        sub.add_argument(
            "--app", help="project app (default: all apps in dependency order)"
        )
    show = actions.add_parser("sql", help="print one migration's SQL")
    show.add_argument("name")
    show.add_argument("--app", help="project app containing the migration")
    return root


def run_plan(args: argparse.Namespace) -> int:
    result = migration_plan(args)
    if not result.operations:
        print("no changes")
    for operation in result.operations:
        print(
            f"{'DESTRUCTIVE ' if operation.destructive else ''}{operation.description}"
        )
    if args.check and result.operations:
        count = len(result.operations)
        noun = "operation" if count == 1 else "operations"
        print(f"pending migrations: {count} {noun}")
        return 1
    if result.path:
        print(f"wrote {result.path}")
    return 0


def migration_plan(args: argparse.Namespace) -> Plan:
    """Build the model/file-state plan for project or legacy CLI usage."""
    manifest = manifest_for(args)
    if manifest is not None:
        return plan_project(args, manifest)
    if not args.models:
        raise MigrationError("pass --models without a project hinoki.toml")
    database_url = args.database or DatabaseSettings().url.reveal()
    return plan(
        models_of(args.models),
        Path(args.dir or "migrations"),
        args.name,
        allow_destructive=args.allow_destructive or args.check,
        write=not args.dry_run and not args.check,
        dialect=database_dialect(database_url),
    )


async def run_database(args: argparse.Namespace) -> None:
    manifest = manifest_for(args)
    if manifest is not None:
        manifest.ordered_apps(args.app)
    database_url = args.database or DatabaseSettings().url.reveal()
    database = await Database.connect(database_url, max_size=1)
    try:
        if manifest is not None:
            await run_database_project(args, manifest, database)
        elif args.action == "apply":
            applied = await migrator.apply(database, Path(args.dir or "migrations"))
            print("\n".join(applied) or "nothing to apply")
        else:
            for name, state in await migrator.status(
                database, Path(args.dir or "migrations")
            ):
                print(f"{state:8} {name}")
    finally:
        database.close()


def run_sql(args: argparse.Namespace) -> None:
    manifest = manifest_for(args)
    if manifest is not None:
        sql_project(args, manifest)
        return
    for path in migration_files(Path(args.dir or "migrations")):
        if path.stem == args.name:
            print(path.read_text(encoding="utf-8"), end="")
            return
    raise MigrationError(f"no migration named {args.name!r}")


def run_new(args: argparse.Namespace) -> int:
    from hinoki.scaffold import ScaffoldError, create  # noqa: PLC0415 - only for `new`

    try:
        target = create(args.name, Path(args.dir), args.hinoki)
    except ScaffoldError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"created {target}; next: cd {target} && uv sync && uv run pytest")
    return 0


def run_mcp_command(args: argparse.Namespace) -> int:
    from hinoki.mcp import run_mcp  # noqa: PLC0415 - loaded only for `mcp`

    return run_mcp(args)


def plan_check_option_error(args: argparse.Namespace) -> str | None:
    """Describe options that are meaningless alongside the read-only check."""
    options = []
    if args.dry_run:
        options.append("--dry-run")
    if args.name is not None:
        options.append("--name")
    if args.allow_destructive:
        options.append("--allow-destructive")
    return ", ".join(options) if options else None


def run_migration_action(args: argparse.Namespace) -> int:
    """Execute one validated migration subcommand."""
    if args.action == "plan":
        return run_plan(args)
    if args.action == "sql":
        run_sql(args)
        return 0
    asyncio.run(run_database(args))
    return 0


def run_migrations(args: argparse.Namespace) -> int:
    if args.action == "plan" and args.check:
        options = plan_check_option_error(args)
        if options is not None:
            print(
                f"error: --check cannot be combined with {options}",
                file=sys.stderr,
            )
            return 2
    try:
        return run_migration_action(args)
    except (MigrationError, ProjectError) as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "  fix: correct the model or migration state; inspect with "
            "`hinoki migrations plan`",
            file=sys.stderr,
        )
        return 1
    except DatabaseError as error:
        print(f"error: database: {error}", file=sys.stderr)
        print(
            f"  fix: check the URL in {URL_VARIABLE} (or --database) and that "
            "the selected database server accepts the connection",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "manage":
        from hinoki.managementcli import run  # noqa: PLC0415 - only for `manage`

        return run(arguments[1:])
    args = parser().parse_args(arguments)
    commands = {
        "check": run_check,
        "new": run_new,
        "app": projectcli.run,
        "doctor": doctorcli.run_doctor,
        "mcp": run_mcp_command,
        "migrations": run_migrations,
        "openapi": openapicli.run_openapi,
        "routes": routescli.run_routes,
        "settings": settingscli.run_settings,
        "shell": shellcli.run,
        "version": versioncli.run_version,
        "dev": serve.run_dev,
        "serve": serve.run_serve,
    }
    return commands[args.group](args)


if __name__ == "__main__":
    sys.exit(main())
