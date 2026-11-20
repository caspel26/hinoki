"""``hinoki migrations plan|apply|status|sql`` and ``check``."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import cast

from hinoki import (
    admincli,
    completioncli,
    doctorcli,
    openapicli,
    projectcli,
    routescli,
    serve,
    settingscli,
    shellcli,
    skillscli,
    versioncli,
)
from hinoki.checkcli import add_arguments, run_check
from hinoki.clihelp import HinokiParser
from hinoki.db import Database, DatabaseError
from hinoki.errors import MigrationError
from hinoki.orm import migrator
from hinoki.orm.migrations import Plan, migration_files, models_of, plan
from hinoki.project import ProjectError, ProjectManifest, resolve_target
from hinoki.projectmigrations import (
    URL_VARIABLE,
    DatabaseResolutionError,
    manifest_for,
    migration_database,
    plan_project,
    run_database_project,
    sql_project,
)
from hinoki.projectmigrations import (
    database_dialect as _database_dialect,
)
from hinoki.reportui import run_timed


def database_dialect(url: str) -> str:
    """Return the SQL dialect selected by a database URL scheme."""
    return _database_dialect(url)


# Commands whose optional TARGET defaults to the manifest's ``project.application``.
TARGETED = frozenset({"check", "dev", "mcp", "openapi", "routes", "serve", "settings"})


def parser() -> argparse.ArgumentParser:
    root = HinokiParser(prog="hinoki", group_commands=True)
    commands = cast(
        "argparse._SubParsersAction[argparse.ArgumentParser]",
        root.add_subparsers(dest="group", required=True, metavar="<command>"),
    )
    add_arguments(commands)
    settingscli.add_arguments(commands)
    routescli.add_arguments(commands)
    openapicli.add_arguments(commands)
    versioncli.add_arguments(commands)
    projectcli.add_arguments(commands)
    admincli.add_arguments(commands)
    doctorcli.add_arguments(commands)
    shellcli.add_arguments(commands)
    skillscli.add_arguments(commands)
    completioncli.add_arguments(commands)
    serve.add_arguments(commands)
    commands.add_parser("manage", help="run an app's management commands")
    group = commands.add_parser(
        "migrations",
        help="migration workflow: plan, apply and inspect",
        description=(
            "Workflow: plan and review migrations, then apply and inspect status."
        ),
    )
    group.add_argument(
        "--dir", help="legacy migrations directory (without hinoki.toml)"
    )
    actions = group.add_subparsers(dest="action")
    planner = actions.add_parser("plan", help="write a migration from the models")
    planner.add_argument("--models", help="legacy module that defines models")
    planner.add_argument("app_name", nargs="?", help="project app to plan")
    planner.add_argument("--app", help="project app to plan (same as the argument)")
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
        help=f"target database URL (default: {URL_VARIABLE}, the app's database "
        "setting, then local SQLite)",
    )
    for name, text in (("apply", "apply pending migrations"), ("status", "list them")):
        sub = actions.add_parser(name, help=text)
        sub.add_argument(
            "--database",
            help=f"connection URL; prefer {URL_VARIABLE} or the app's database "
            "setting, which keep a password out of the process list",
        )
        sub.add_argument(
            "app_name",
            nargs="?",
            help="project app and its prerequisites (default: all apps in order)",
        )
        sub.add_argument("--app", help="project app (same as the argument)")
    show = actions.add_parser("sql", help="print one migration's SQL")
    show.add_argument("name")
    show.add_argument(
        "--app", help="project app containing the migration (default: search all)"
    )
    group.set_defaults(group_help=group.format_help())
    return root


def run_plan(args: argparse.Namespace) -> int:
    results = migration_plans(args)
    if len(results) == 1:
        return print_plan_result(args, results[0][1])
    return print_all_app_plans(args, results)


def print_plan_result(args: argparse.Namespace, result: Plan) -> int:
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


def print_all_app_plans(
    args: argparse.Namespace, results: list[tuple[str | None, Plan]]
) -> int:
    operation_count = sum(len(result.operations) for _, result in results)
    for app_name, result in results:
        print_app_plan(app_name, result)
    return summarize_all_app_plans(args, operation_count)


def print_app_plan(app_name: str | None, result: Plan) -> None:
    print(f"{app_name}:")
    if not result.operations:
        print("  no changes")
    for operation in result.operations:
        destructive = "DESTRUCTIVE " if operation.destructive else ""
        print(f"  {destructive}{operation.description}")


def summarize_all_app_plans(args: argparse.Namespace, operation_count: int) -> int:
    if args.check:
        if operation_count:
            noun = "operation" if operation_count == 1 else "operations"
            print(f"pending migrations: {operation_count} {noun} across apps")
            return 1
        print("no migrations are pending")
        return 0
    if operation_count:
        noun = "operation" if operation_count == 1 else "operations"
        print(
            f"all-app preview only: {operation_count} {noun}; no files written. "
            "Prerequisite changes shown here may not be ready to apply."
        )
    else:
        print("no changes across registered apps")
    return 0


def migration_plan(args: argparse.Namespace) -> Plan:
    """Build the model/file-state plan for project or legacy CLI usage."""
    return migration_plans(args)[0][1]


def migration_plans(args: argparse.Namespace) -> list[tuple[str | None, Plan]]:
    """Build one focused plan or a read-only preview for every project app."""
    manifest = manifest_for(args)
    if manifest is not None:
        if args.app is None and len(manifest.apps) > 1 and (args.check or args.dry_run):
            if args.name is not None:
                raise MigrationError("--name requires --app for a multi-app preview")
            dialect = plan_dialect(args, manifest)
            return [
                (
                    app.name,
                    plan_project(
                        args,
                        manifest,
                        app.name,
                        all_app_preview=True,
                        dialect=dialect,
                    ),
                )
                for app in manifest.ordered_apps()
            ]
        return [focused_plan(args, manifest)]
    if not args.models:
        raise MigrationError("pass --models without a project hinoki.toml")
    result = plan(
        models_of(args.models),
        Path(args.dir or "migrations"),
        args.name,
        allow_destructive=args.allow_destructive or args.check or args.dry_run,
        write=not args.dry_run and not args.check,
        dialect=plan_dialect(args, manifest),
    )
    return [(None, result)]


def plan_dialect(args: argparse.Namespace, manifest: ProjectManifest | None) -> str:
    """Resolve and report the database whose SQL dialect the plan uses."""
    target = migration_database(args, manifest)
    print(target.describe(), file=sys.stderr)
    return database_dialect(target.url)


def focused_plan(
    args: argparse.Namespace, manifest: ProjectManifest
) -> tuple[str, Plan]:
    """Plan the one selected app; several apps and no name ask for the name."""
    if args.app is None and len(manifest.apps) > 1:
        names = ", ".join(app.name for app in manifest.ordered_apps())
        raise ProjectError(
            f"this project has multiple apps ({names}); name one with "
            "`hinoki migrations plan <app>`"
        )
    app = manifest.app(args.app)
    return app.name, plan_project(args, manifest, dialect=plan_dialect(args, manifest))


async def run_database(args: argparse.Namespace) -> None:
    manifest = manifest_for(args)
    migration_directories: dict[str, Path] = {}
    if manifest is not None:
        manifest.ordered_apps(args.app)
        migration_directories = manifest.migration_directories()
    target = migration_database(args, manifest)
    print(target.describe(), file=sys.stderr)
    database = await Database.connect(target.url, max_size=1)
    try:
        if manifest is not None:
            await run_database_project(args, manifest, database, migration_directories)
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
    from hinoki.newcli import run_new as new_project  # noqa: PLC0415 - only for `new`

    return new_project(args)


def run_mcp_command(args: argparse.Namespace) -> int:
    from hinoki.ai.mcp import run_mcp  # noqa: PLC0415 - loaded only for `mcp`

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
        return run_timed("planning", "planned", lambda: run_plan(args))
    if args.action == "sql":
        run_sql(args)
        return 0
    if args.action == "apply":
        return run_timed("applying", "applied", lambda: run_database_action(args))
    return run_database_action(args)


def run_database_action(args: argparse.Namespace) -> int:
    asyncio.run(run_database(args))
    return 0


def run_migrations(args: argparse.Namespace) -> int:
    if args.action is None:
        print(args.group_help, end="")
        return 0
    named = getattr(args, "app_name", None)
    if named is not None and args.app not in (None, named):
        print(
            f"error: app {named!r} conflicts with --app {args.app!r}", file=sys.stderr
        )
        return 2
    if named is not None:
        args.app = named
    if args.action == "plan" and args.check:
        options = plan_check_option_error(args)
        if options is not None:
            print(
                f"error: --check cannot be combined with {options}",
                file=sys.stderr,
            )
            return 2
    return run_migrations_with_error_handling(args)


def run_migrations_with_error_handling(args: argparse.Namespace) -> int:
    try:
        return run_migration_action(args)
    except DatabaseResolutionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
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
            f"  fix: check the database named above ({URL_VARIABLE}, --database or "
            "the app's database setting) and that the selected database server "
            "accepts the connection",
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
        "admin": admincli.run,
        "app": projectcli.run,
        "doctor": doctorcli.run_doctor,
        "mcp": run_mcp_command,
        "migrations": run_migrations,
        "openapi": openapicli.run_openapi,
        "routes": routescli.run_routes,
        "settings": settingscli.run_settings,
        "shell": shellcli.run,
        "skills": skillscli.run,
        "version": versioncli.run_version,
        "dev": serve.run_dev,
        "serve": serve.run_serve,
    }
    if args.group == "completion":
        return completioncli.run(args, parser())
    if args.group in TARGETED:
        try:
            args.target = resolve_target(args.target)
        except ProjectError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    return commands[args.group](args)


if __name__ == "__main__":
    sys.exit(main())
