"""Manifest-aware migration command helpers."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hinoki import jsontext
from hinoki.cli.check import build_app
from hinoki.db import Database, DatabaseModule
from hinoki.errors import MigrationError
from hinoki.orm import Model, migrator
from hinoki.orm.migrations import (
    EMPTY,
    Plan,
    State,
    check_constraint_names,
    family,
    migration_files,
    plan,
    state_of,
    table_state,
    validate_migration_state,
)
from hinoki.project import (
    ProjectError,
    ProjectManifest,
    add_project_root,
    find_manifest_or_none,
    load_manifest,
)
from hinoki.settings import (
    LOCAL_DATABASE_URL,
    AdminSettings,
    DatabaseSettings,
    SettingsError,
)

URL_VARIABLE = "HINOKI_DATABASE_URL"
DEFAULT_SOURCE = "local SQLite default"
UNPARSEABLE = "<unparseable URL>"
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")


class DatabaseResolutionError(ProjectError):
    """The database a project's migrations target cannot be determined."""


@dataclass(frozen=True)
class MigrationDatabase:
    """The database a migration command targets and where it was configured."""

    url: str
    source: str

    def describe(self) -> str:
        """One line naming the database and its source, without credentials."""
        return f"database: {self.display()}"

    def display(self) -> str:
        """The masked URL and its source."""
        return f"{redacted_url(self.url)} (from {self.source})"


def redacted_url(url: str) -> str:
    """The URL with everything before its last ``@`` and its options masked.

    Text that does not parse cleanly is not echoed at all.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or not _SCHEME.match(scheme):
        return UNPARSEABLE
    if any(char.isspace() for char in rest):
        return f"{scheme}://{UNPARSEABLE}"
    _credentials, at, location = rest.rpartition("@")
    try:
        parts = urlsplit(f"{scheme}://{location}")
        _ = parts.port
    except ValueError:
        return f"{scheme}://{UNPARSEABLE}"
    masked = "***@" if at else ""
    options = "?..." if parts.query or parts.fragment else ""
    return f"{scheme}://{masked}{parts.netloc}{parts.path}{options}"


def migration_database(
    args: argparse.Namespace, manifest: ProjectManifest | None
) -> MigrationDatabase:
    """Resolve ``--database``, then ``HINOKI_DATABASE_URL``, then the project
    application's database (an installed ``DatabaseModule`` or its ``database``
    setting), then the local SQLite default.

    Raises ``DatabaseResolutionError`` when a project's application cannot be
    loaded or its database cannot be told, rather than guessing a database.
    """
    if getattr(args, "database", None):
        return MigrationDatabase(args.database, "--database")
    environment = os.environ.get(URL_VARIABLE)
    if environment:
        return MigrationDatabase(environment, URL_VARIABLE)
    return _resolved_database(manifest, _load_application)


def _resolved_database(
    manifest: ProjectManifest | None, load: Any
) -> MigrationDatabase:
    configured = _application_database(load(manifest)) if manifest is not None else None
    url, source = configured or (DatabaseSettings().url.reveal(), DEFAULT_SOURCE)
    return MigrationDatabase(_anchor_sqlite(url, manifest), source)


def migration_database_and_admin(
    args: argparse.Namespace, manifest: ProjectManifest | None
) -> tuple[MigrationDatabase, AdminSettings | None]:
    """The database of :func:`migration_database` and the application's admin settings.

    The application is built once. With ``--database`` or the URL variable the
    settings are read best-effort: an application that cannot be built then
    yields no settings, not an error.
    """
    explicit = getattr(args, "database", None) or os.environ.get(URL_VARIABLE)
    if manifest is None:
        return migration_database(args, manifest), None
    try:
        app = _load_application(manifest)
    except DatabaseResolutionError:
        if not explicit:
            raise
        return migration_database(args, manifest), None
    if explicit:
        return migration_database(args, manifest), _admin_settings(app)
    database = _resolved_database(manifest, lambda _manifest: app)
    return database, _admin_settings(app)


def migration_database_url(
    args: argparse.Namespace, manifest: ProjectManifest | None
) -> str:
    """The URL of :func:`migration_database`."""
    return migration_database(args, manifest).url


def _application_database(app: Any) -> tuple[str, str] | None:
    """The URL and source of the application's database, if it declares one.

    The application is built from the project root, so relative settings files
    such as ``.env`` are the ones the server reads when started there. Building
    it reads the settings but opens no connection; its output goes to stderr.
    """
    module = next(
        (m for m in getattr(app, "modules", ()) if m.name == DatabaseModule.name),
        None,
    )
    if module is None or module is getattr(app, "_automatic_database", None):
        return _settings_database(app)
    if isinstance(module, DatabaseModule) and isinstance(
        getattr(module, "url", None), str
    ):
        return module.url, f"application: {type(module).__name__}"
    raise DatabaseResolutionError(
        f"cannot tell which database the application's {type(module).__name__} "
        f"module uses; choose it with --database or {URL_VARIABLE}"
    )


def _admin_settings(app: Any) -> AdminSettings | None:
    group = getattr(app.settings, "admin", None)
    return group if isinstance(group, AdminSettings) else None


def _load_application(manifest: ProjectManifest) -> Any:
    add_project_root(manifest)
    target = manifest.application
    try:
        with (
            contextlib.chdir(manifest.root),
            contextlib.redirect_stdout(sys.stderr),
        ):
            return build_app(target)
    except (Exception, SystemExit) as error:  # never guess another database
        raise DatabaseResolutionError(
            f"{_failure(target, error)}; fix it, or choose the database with "
            f"--database or {URL_VARIABLE}"
        ) from None


def _settings_database(app: Any) -> tuple[str, str] | None:
    group = getattr(app.settings, "database", None)
    if not isinstance(group, DatabaseSettings):
        return None
    url = group.url.reveal()
    if url == LOCAL_DATABASE_URL:
        return None
    return url, f"settings: {type(app.settings).__name__}.database"


def _failure(target: str, error: BaseException) -> str:
    # Settings errors name variables but never values; other messages may not,
    # so only the failing module or attribute name is shown.
    kind = type(error).__name__
    if isinstance(error, SettingsError):
        return f"cannot load the application's settings ({error})"
    if isinstance(error, ImportError | AttributeError) and error.name:
        return f"cannot import the application {target} ({kind}: {error.name!r})"
    return f"cannot load the application {target} ({kind})"


def _anchor_sqlite(url: str, manifest: ProjectManifest | None) -> str:
    """Root a relative SQLite path at the project, not the working directory."""
    if manifest is None or not url.startswith("sqlite://"):
        return url
    location = url.removeprefix("sqlite://")
    if location.partition("?")[0] == ":memory:":
        raise DatabaseResolutionError(
            "the database is an in-memory SQLite database, which "
            "migrations cannot keep; configure a file or server database"
        )
    path = Path(location)
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


def plan_project(
    args: argparse.Namespace,
    manifest: ProjectManifest,
    app_name: str | None = None,
    *,
    all_app_preview: bool = False,
    dialect: str | None = None,
) -> Plan:
    app = manifest.app(args.app if app_name is None else app_name)
    if not app.has_persistence:
        raise MigrationError(f"app {app.name!r} has no models or migrations")
    models = manifest.all_models()
    # Constraint names share one database namespace across every app.
    check_constraint_names(
        {m.__table__: table_state(m) for group in models.values() for m in group}
    )
    directories = manifest.migration_directories()
    dependencies = manifest.persistent_apps(app.name)
    external = [
        model
        for dependency in dependencies
        if dependency.name != app.name
        for model in models[dependency.name]
    ]
    history = _project_migration_history(manifest, directories)
    if dialect is None:
        dialect = database_dialect(migration_database_url(args, manifest))
    candidate = state_of(
        models[app.name],
        dialect,
        reference_models=external,
    )
    latest = {name: states[-1] if states else EMPTY for name, states in history.items()}
    owners = _table_history_owners(history)
    _add_current_table_owners(owners, models)
    if not all_app_preview:
        _validate_recorded_prerequisites(app.name, candidate, owners, latest)
    _reject_cross_app_teardown(app.name, latest[app.name], candidate, owners, history)
    return plan(
        models[app.name],
        directories[app.name],
        args.name,
        allow_destructive=args.allow_destructive or args.check or args.dry_run,
        write=not args.dry_run and not args.check,
        dialect=dialect,
        reference_models=external,
    )


def _project_migration_history(
    manifest: ProjectManifest, directories: dict[str, Path]
) -> dict[str, list[State]]:
    return {
        app.name: _migration_history(app.name, directories[app.name])
        for app in manifest.persistent_apps()
    }


def _migration_history(app_name: str, directory: Path) -> list[State]:
    history: list[State] = []
    for migration in migration_files(directory):
        state_path = migration.with_suffix(".state.json")
        try:
            state = jsontext.loads(state_path.read_bytes())
        except OSError as error:
            raise MigrationError(
                f"app {app_name!r}: cannot read {state_path.name}; restore it "
                "from version control"
            ) from error
        except ValueError as error:
            raise MigrationError(
                f"app {app_name!r}: {state_path.name} is not valid JSON"
            ) from error
        _validate_migration_state(app_name, state_path.name, state)
        history.append(state)
    return history


def _validate_migration_state(app_name: str, filename: str, state: object) -> None:
    try:
        validate_migration_state(state, filename)
    except MigrationError as error:
        raise MigrationError(f"app {app_name!r}: {error}") from error


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
    if args.app is not None and not manifest.app(args.app).has_persistence:
        raise MigrationError(f"app {args.app!r} has no models or migrations")
    apps = manifest.persistent_apps(args.app)
    if not apps:
        print("no apps with migrations")
        return
    for app in apps:
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
    """Print one migration; without ``--app`` search every app for the name."""
    if args.app is not None:
        target = manifest.app(args.app)
        if not target.has_persistence:
            raise MigrationError(f"app {args.app!r} has no models or migrations")
        apps = [target.name]
    else:
        apps = [app.name for app in manifest.persistent_apps()]
    directories = manifest.migration_directories()
    found = [
        (name, path)
        for name in apps
        for path in migration_files(directories[name])
        if path.stem == args.name
    ]
    if len(found) == 1:
        print(found[0][1].read_text(encoding="utf-8"), end="")
        return
    if found:
        owners = ", ".join(name for name, _path in found)
        raise MigrationError(
            f"migration {args.name!r} exists in apps {owners}; "
            "choose one with --app <app>"
        )
    if len(apps) == 1:
        raise MigrationError(f"app {apps[0]!r} has no migration named {args.name!r}")
    raise MigrationError(f"no app has a migration named {args.name!r}")
