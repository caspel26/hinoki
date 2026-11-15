"""Manifest-aware migration command helpers."""

from __future__ import annotations

import argparse

from hinoki.db import Database
from hinoki.errors import MigrationError
from hinoki.orm import migrator
from hinoki.orm.migrations import migration_files, plan
from hinoki.project import (
    ProjectError,
    ProjectManifest,
    find_manifest_or_none,
    load_manifest,
)
from hinoki.settings import DatabaseSettings


def manifest_for(args: argparse.Namespace) -> ProjectManifest | None:
    """Resolve project mode and reject flags that would override its registry."""
    path = find_manifest_or_none()
    if path is None:
        if args.app:
            raise ProjectError("--app requires a project hinoki.toml")
        return None
    if getattr(args, "models", None) or args.dir:
        raise ProjectError(
            "do not combine project manifest settings with --models or --dir"
        )
    return load_manifest(path)


def plan_project(args: argparse.Namespace, manifest: ProjectManifest) -> None:
    app = manifest.app(args.app)
    models = manifest.all_models()
    dependencies = manifest.ordered_apps(app.name)
    external = [
        model
        for dependency in dependencies
        if dependency.name != app.name
        for model in models[dependency.name]
    ]
    result = plan(
        models[app.name],
        manifest.migrations_path(app),
        args.name,
        allow_destructive=args.allow_destructive,
        write=not args.dry_run,
        dialect=_dialect(args),
        reference_models=external,
    )
    if not result.operations:
        print("no changes")
    for operation in result.operations:
        print(
            f"{'DESTRUCTIVE ' if operation.destructive else ''}{operation.description}"
        )
    if result.path:
        print(f"wrote {result.path}")


async def run_database_project(
    args: argparse.Namespace, manifest: ProjectManifest, database: Database
) -> None:
    for app in manifest.ordered_apps(args.app):
        directory = manifest.migrations_path(app)
        namespace = manifest.migration_namespace(app)
        if args.action == "apply":
            applied = await migrator.apply(database, directory, namespace=namespace)
            print(f"{app.name}: " + (", ".join(applied) or "nothing to apply"))
        else:
            for name, state in await migrator.status(
                database, directory, namespace=namespace
            ):
                print(f"{app.name:16} {state:8} {name}")


def sql_project(args: argparse.Namespace, manifest: ProjectManifest) -> None:
    app = manifest.app(args.app)
    for path in migration_files(manifest.migrations_path(app)):
        if path.stem == args.name:
            print(path.read_text(encoding="utf-8"), end="")
            return
    raise MigrationError(f"app {app.name!r} has no migration named {args.name!r}")


def _dialect(args: argparse.Namespace) -> str:
    """Use the selected connection URL for migration SQL, including SQLite default."""
    url = args.database or DatabaseSettings().url.reveal()
    schemes = {
        "sqlite://": "sqlite",
        "mysql://": "mysql",
        "mariadb://": "mariadb",
        "postgres://": "postgres",
        "postgresql://": "postgres",
    }
    dialect = next(
        (value for scheme, value in schemes.items() if url.startswith(scheme)), None
    )
    if dialect is None:
        raise MigrationError("database URL must use a supported backend scheme")
    return dialect
