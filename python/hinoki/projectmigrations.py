"""Manifest-aware migration command helpers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from hinoki.db import Database
from hinoki.errors import MigrationError
from hinoki.orm import Model, migrator
from hinoki.orm.migrations import (
    EMPTY,
    LEGACY_STATE_TYPES,
    Plan,
    State,
    family,
    migration_files,
    plan,
    state_of,
)
from hinoki.project import (
    ProjectError,
    ProjectManifest,
    find_manifest_or_none,
    load_manifest,
)
from hinoki.settings import DatabaseSettings

URL_VARIABLE = "HINOKI_DATABASE_URL"


def migration_database_url(
    args: argparse.Namespace, manifest: ProjectManifest | None
) -> str:
    """Resolve CLI/environment/default DB input, anchoring implicit project SQLite."""
    explicit = args.database or os.environ.get(URL_VARIABLE)
    if explicit:
        return explicit

    url = DatabaseSettings().url.reveal()
    if manifest is None or not url.startswith("sqlite://"):
        return url
    path = Path(url.removeprefix("sqlite://"))
    if path.is_absolute():
        return url
    return f"sqlite://{(manifest.root / path).resolve()}"


def database_dialect(url: str) -> str:
    """Return the SQL dialect selected by a database URL scheme."""
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


def plan_project(args: argparse.Namespace, manifest: ProjectManifest) -> Plan:
    app = manifest.app(args.app)
    models = manifest.all_models()
    directories = manifest.migration_directories()
    dependencies = manifest.ordered_apps(app.name)
    external = [
        model
        for dependency in dependencies
        if dependency.name != app.name
        for model in models[dependency.name]
    ]
    history = _project_migration_history(manifest, directories)
    candidate = state_of(
        models[app.name],
        database_dialect(migration_database_url(args, manifest)),
        reference_models=external,
    )
    latest = {name: states[-1] if states else EMPTY for name, states in history.items()}
    owners = _table_history_owners(history)
    _add_current_table_owners(owners, models)
    _validate_recorded_prerequisites(app.name, candidate, owners, latest)
    _reject_cross_app_teardown(app.name, latest[app.name], candidate, owners, history)
    return plan(
        models[app.name],
        directories[app.name],
        args.name,
        allow_destructive=args.allow_destructive or args.check,
        write=not args.dry_run and not args.check,
        dialect=database_dialect(migration_database_url(args, manifest)),
        reference_models=external,
    )


def _project_migration_history(
    manifest: ProjectManifest, directories: dict[str, Path]
) -> dict[str, list[State]]:
    return {
        app.name: _migration_history(app.name, directories[app.name])
        for app in manifest.ordered_apps()
    }


def _migration_history(app_name: str, directory: Path) -> list[State]:
    history: list[State] = []
    for migration in migration_files(directory):
        state_path = migration.with_suffix(".state.json")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise MigrationError(
                f"app {app_name!r}: cannot read {state_path.name}; restore it "
                "from version control"
            ) from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise MigrationError(
                f"app {app_name!r}: {state_path.name} is not valid JSON"
            ) from error
        _validate_migration_state(app_name, state_path.name, state)
        history.append(state)
    return history


def _validate_migration_state(app_name: str, filename: str, state: object) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("tables"), dict):
        raise MigrationError(
            f"app {app_name!r}: {filename} must contain a tables object"
        )
    for table, spec in state["tables"].items():
        _validate_table_state(app_name, filename, table, spec)


def _validate_table_state(
    app_name: str, filename: str, table: object, spec: object
) -> None:
    if not isinstance(table, str) or not isinstance(spec, dict):
        raise MigrationError(f"app {app_name!r}: {filename} has an invalid table entry")
    columns = spec.get("columns")
    if not isinstance(columns, dict):
        raise MigrationError(
            f"app {app_name!r}: {filename} has invalid columns for {table!r}"
        )
    for name, column in columns.items():
        _validate_column_state(app_name, filename, table, name, column)


def _validate_column_state(
    app_name: str, filename: str, table: str, name: object, column: object
) -> None:
    if not isinstance(name, str) or not isinstance(column, dict):
        raise MigrationError(
            f"app {app_name!r}: {filename} has invalid column state for {table}.{name}"
        )
    if not isinstance(column.get("type"), str) or not isinstance(
        column.get("primary_key"), bool
    ):
        raise MigrationError(
            f"app {app_name!r}: {filename} has invalid column state for {table}.{name}"
        )
    column["type"] = LEGACY_STATE_TYPES.get(column["type"].casefold(), column["type"])
    reference = column.get("references")
    if reference is not None and (
        not isinstance(reference, dict)
        or not isinstance(reference.get("table"), str)
        or not isinstance(reference.get("column"), str)
    ):
        raise MigrationError(
            f"app {app_name!r}: {filename} has invalid column state for {table}.{name}"
        )


def _table_history_owners(
    history: dict[str, list[State]],
) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for app_name, states in history.items():
        for state in states:
            for table in state["tables"]:
                owners.setdefault(table, set()).add(app_name)
    return owners


def _add_current_table_owners(
    owners: dict[str, set[str]], models: dict[str, list[type[Model]]]
) -> None:
    for app_name, app_models in models.items():
        for model in app_models:
            owners.setdefault(model.__table__, set()).add(app_name)


def _validate_recorded_prerequisites(
    app_name: str,
    candidate: State,
    owners: dict[str, set[str]],
    latest: dict[str, State],
) -> None:
    for table, spec in candidate["tables"].items():
        for name, column in spec["columns"].items():
            target = column["references"]
            if target is None:
                continue
            target_app = _external_owner(app_name, target["table"], owners)
            if target_app is None:
                continue
            _check_recorded_target(
                app_name, target_app, table, name, column, target, latest[target_app]
            )


def _external_owner(
    app_name: str, table: str, owners: dict[str, set[str]]
) -> str | None:
    candidates = owners.get(table, set()) - {app_name}
    if not candidates:
        return None
    if len(candidates) != 1:
        raise MigrationError(f"table {table!r} has ambiguous migration ownership")
    return next(iter(candidates))


def _check_recorded_target(
    app_name: str,
    target_app: str,
    table: str,
    name: str,
    column: dict[str, Any],
    target: dict[str, str],
    recorded: State,
) -> None:
    table_state = recorded["tables"].get(target["table"])
    target_column = (
        table_state["columns"].get(target["column"])
        if table_state is not None
        else None
    )
    if target_column is None:
        raise MigrationError(
            f"{app_name}.{table}.{name} depends on {target_app}."
            f"{target['table']}.{target['column']}, absent from recorded migrations; "
            f"plan {target_app} first, review it, then plan {app_name}"
        )
    if not target_column["primary_key"] or family(target_column["type"]) != family(
        column["type"]
    ):
        raise MigrationError(
            f"{app_name}.{table}.{name} is incompatible with recorded target "
            f"{target_app}.{target['table']}.{target['column']}; plan the "
            f"{target_app} migration first"
        )


def _reject_cross_app_teardown(
    app_name: str,
    recorded: State,
    candidate: State,
    owners: dict[str, set[str]],
    history: dict[str, list[State]],
) -> None:
    for child, table, column in _historical_cross_app_references(
        app_name, owners, history
    ):
        old = _recorded_column(recorded, table, column)
        if old is None:
            continue
        new = _recorded_column(candidate, table, column)
        if _target_was_removed_or_changed(old, new):
            raise MigrationError(
                f"cannot plan this change: recorded migrations for app {child!r} "
                f"reference {app_name}.{table}.{column}; remove the child foreign "
                "key in a separate reviewed deployment phase before planning this "
                "parent teardown"
            )


def _historical_cross_app_references(
    app_name: str,
    owners: dict[str, set[str]],
    history: dict[str, list[State]],
) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for child, states in history.items():
        if child == app_name:
            continue
        for state in states:
            found.update(_state_cross_app_references(child, app_name, owners, state))
    return found


def _state_cross_app_references(
    child: str, app_name: str, owners: dict[str, set[str]], state: State
) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for spec in state["tables"].values():
        for column in spec["columns"].values():
            target = column["references"]
            if target is not None and app_name in owners.get(target["table"], set()):
                found.add((child, target["table"], target["column"]))
    return found


def _recorded_column(state: State, table: str, column: str) -> dict[str, Any] | None:
    table_state = state["tables"].get(table)
    return table_state["columns"].get(column) if table_state is not None else None


def _target_was_removed_or_changed(
    old: dict[str, Any], new: dict[str, Any] | None
) -> bool:
    return (
        new is None
        or not new["primary_key"]
        or family(old["type"]) != family(new["type"])
    )


async def run_database_project(
    args: argparse.Namespace,
    manifest: ProjectManifest,
    database: Database,
    directories: dict[str, Path],
) -> None:
    for app in manifest.ordered_apps(args.app):
        directory = directories[app.name]
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
    directories = manifest.migration_directories()
    for path in migration_files(directories[app.name]):
        if path.stem == args.name:
            print(path.read_text(encoding="utf-8"), end="")
            return
    raise MigrationError(f"app {app.name!r} has no migration named {args.name!r}")
