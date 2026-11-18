"""Explicit project and modular-app registry in ``hinoki.toml``."""

from __future__ import annotations

import dataclasses
import importlib
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import cast

from hinoki.orm import MARK, FieldMeta, Model
from hinoki.orm.migrations import models_of
from hinoki.orm.naming import MIGRATION_LEDGER, table_names_conflict
from hinoki.routing import Includable, Mount
from hinoki.scaffold import NAME, RESERVED_NAMES

_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectApp:
    """One app's model module, migration package and app dependencies."""

    name: str
    models: str
    migrations: str
    requires: tuple[str, ...]
    routes: str = ""  # ``module:attribute`` of the app's list of routes
    prefix: str = ""  # where the routes are mounted, ``/<name>`` when empty


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectManifest:
    """Validated project metadata loaded from a local TOML manifest."""

    path: Path
    application: str
    apps: tuple[ProjectApp, ...]
    routes: str = ""  # ``module:attribute`` of the project's own routes

    @property
    def root(self) -> Path:
        return self.path.parent

    def ordered_apps(self, selected: str | None = None) -> tuple[ProjectApp, ...]:
        """Return stable dependency order, optionally including prerequisites."""
        by_name = {app.name: app for app in self.apps}
        if selected is None:
            wanted = set(by_name)
        else:
            if selected not in by_name:
                raise ProjectError(f"unknown app {selected!r} in {self.path.name}")
            wanted = set()
            dependency_stack = [selected]
            while dependency_stack:
                name = dependency_stack.pop()
                if name not in wanted:
                    wanted.add(name)
                    dependency_stack.extend(by_name[name].requires)

        result: list[ProjectApp] = []
        pending = set(wanted)
        while pending:
            ready = [
                app
                for app in self.apps
                if app.name in pending
                and all(dependency not in pending for dependency in app.requires)
            ]
            if not ready:
                raise ProjectError("app dependencies contain a cycle")
            for app in ready:
                result.append(app)
                pending.remove(app.name)
        return tuple(result)

    def app(self, name: str | None = None) -> ProjectApp:
        """Resolve one app, requiring a name when the project is modular."""
        if name is None:
            if len(self.apps) != 1:
                raise ProjectError("pass --app because this project has multiple apps")
            return self.apps[0]
        found = next((app for app in self.apps if app.name == name), None)
        if found is None:
            raise ProjectError(f"unknown app {name!r} in {self.path.name}")
        return found

    def migrations_path(self, app: ProjectApp) -> Path:
        """Resolve an app's migration package only when a CLI command needs it."""
        add_project_root(self)
        try:
            module = importlib.import_module(app.migrations)
        except ImportError as error:
            raise ProjectError(
                f"cannot import migration package {app.migrations!r}: {error}"
            ) from None
        paths = getattr(module, "__path__", None)
        if not paths:
            raise ProjectError(
                f"app {app.name!r} migrations must name a Python package"
            )
        return Path(next(iter(paths)))

    def migration_directories(self) -> dict[str, Path]:
        """Resolve every registered migration package and reject aliases/overlap."""
        resolved: list[tuple[str, Path]] = []
        for app in self.ordered_apps():
            try:
                directory = self.migrations_path(app).resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ProjectError(
                    f"cannot resolve migrations directory for app {app.name!r}"
                ) from error
            if not directory.is_dir():
                raise ProjectError(
                    f"migrations package for app {app.name!r} is not a directory"
                )
            for other_name, other_directory in resolved:
                if _migration_directories_conflict(directory, other_directory):
                    raise ProjectError(
                        f"apps {other_name!r} and {app.name!r} use the same or "
                        "overlapping migrations directories; choose separate packages"
                    )
            resolved.append((app.name, directory))
        return dict(resolved)

    def migration_namespace(self, app: ProjectApp) -> str:
        """Keep the scaffolded root app compatible with the legacy ledger."""
        root_package = self.application.partition(":")[0].split(".", maxsplit=1)[0]
        return "" if app.name == root_package else app.name

    def route_items(self) -> list[Includable]:
        """The project's routes, then each app's routes mounted at its prefix.

        Apps follow the dependency order; an app without ``routes`` adds nothing.
        Importing the route modules is the only effect.
        """
        add_project_root(self)
        items: list[Includable] = []
        if self.routes:
            items.extend(self._route_list(self.routes, "project"))
        for app in self.ordered_apps():
            if app.routes:
                mine = self._route_list(app.routes, f"app {app.name!r}")
                if mine:
                    items.append(Mount(app.prefix or f"/{app.name}", *mine))
        return items

    def _route_list(self, target: str, owner: str) -> list[Includable]:
        module_name, _, attribute = target.partition(":")
        try:
            found = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as error:
            raise ProjectError(
                f"cannot load the routes of {owner} ({target}): {error}"
            ) from None
        if not isinstance(found, list | tuple):
            raise ProjectError(
                f"the routes of {owner} ({target}) must be a list of routes"
            )
        return cast(list[Includable], list(found))

    def models(self, app: ProjectApp) -> list[type[Model]]:
        """Load this app's concrete model classes without opening resources."""
        add_project_root(self)
        try:
            return models_of(app.models)
        except (ImportError, AttributeError) as error:
            raise ProjectError(
                f"cannot import models for app {app.name!r} ({app.models}): {error}"
            ) from None

    def all_models(self) -> dict[str, list[type[Model]]]:
        """Load models in manifest order and reject ambiguous table ownership."""
        result: dict[str, list[type[Model]]] = {}
        owners: dict[str, str] = {}
        registered: list[tuple[type[Model], str]] = []
        for app in self.ordered_apps():
            models = self.models(app)
            for model in models:
                if model.__table_problem__:
                    raise ProjectError(model.__table_problem__)
                if model.__table__.casefold() == MIGRATION_LEDGER:
                    raise ProjectError(
                        f"{model.__module__}.{model.__qualname__} claims Hinoki's "
                        f"migration ledger table {MIGRATION_LEDGER!r}; choose a "
                        "distinct application table"
                    )
                collision = next(
                    (
                        (previous, owner)
                        for previous, owner in registered
                        if table_names_conflict(previous, model)
                    ),
                    None,
                )
                if collision is not None:
                    previous, owner = collision
                    raise ProjectError(
                        f"table {model.__table__!r} is declared by "
                        f"{previous.__module__}.{previous.__qualname__} in app "
                        f"{owner!r} and {model.__module__}.{model.__qualname__} "
                        f"in app {app.name!r}; choose distinct table= values"
                    )
                registered.append((model, app.name))
                owners[model.__table__] = app.name
            result[app.name] = models
        self._check_model_dependencies(result, owners)
        return result

    def _check_model_dependencies(
        self,
        app_models: dict[str, list[type[Model]]],
        owners: dict[str, str],
    ) -> None:
        for app_name, models in app_models.items():
            allowed = {item.name for item in self.ordered_apps(app_name)}
            _check_app_model_references(app_name, models, owners, allowed)


def _migration_directories_conflict(left: Path, right: Path) -> bool:
    if left == right or left in right.parents or right in left.parents:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError as error:
        raise ProjectError(
            "cannot compare registered migrations directories"
        ) from error


def _check_app_model_references(
    app_name: str,
    models: list[type[Model]],
    owners: dict[str, str],
    allowed: set[str],
) -> None:
    for model in models:
        for item in dataclasses.fields(model):  # type: ignore[arg-type]
            meta = item.metadata.get(MARK, FieldMeta())
            if meta.references is not None:
                _check_reference_owner(
                    model, item.name, meta.references, owners, allowed, app_name
                )


def _check_reference_owner(
    model: type[Model],
    field_name: str,
    reference: type[Model],
    owners: dict[str, str],
    allowed: set[str],
    app_name: str,
) -> None:
    table = reference.__table__
    owner = owners.get(table)
    if owner is None:
        raise ProjectError(
            f"{model.__name__}.{field_name} references table {table!r} "
            "not registered in hinoki.toml"
        )
    if owner not in allowed:
        raise ProjectError(
            f"app {app_name!r} references table {table!r} from app {owner!r}; "
            f"add {owner!r} to its requires list"
        )


class ProjectError(ValueError):
    """The project manifest is missing, malformed or inconsistent."""


def find_manifest(start: Path | None = None) -> Path:
    """Find ``hinoki.toml`` from the working directory or an ancestor."""
    found = find_manifest_or_none(start)
    if found is not None:
        return found
    raise ProjectError("no hinoki.toml found; run `hinoki new` or pass a target")


def find_manifest_or_none(start: Path | None = None) -> Path | None:
    """Find a project manifest if one exists, without treating absence as error."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "hinoki.toml"
        if candidate.is_file():
            return candidate
    return None


def load_manifest(path: Path | None = None) -> ProjectManifest:
    """Parse and validate a manifest without importing app code."""
    manifest_path = path or find_manifest()
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ProjectError(f"cannot read {manifest_path.name}: {error}") from None
    project = data.get("project")
    if not isinstance(project, dict):
        raise ProjectError("hinoki.toml needs a [project] table")
    application = project.get("application")
    if not isinstance(application, str) or not _valid_target(application):
        raise ProjectError("project.application must be a module:attribute target")
    raw_apps = data.get("apps", [])
    if not isinstance(raw_apps, list):
        raise ProjectError("apps must be an array of [[apps]] tables")
    apps = tuple(_parse_app(item) for item in raw_apps)
    names = [app.name for app in apps]
    if len(set(names)) != len(names):
        raise ProjectError("app names must be unique")
    known = set(names)
    for app in apps:
        unknown = set(app.requires) - known
        if unknown:
            raise ProjectError(
                f"app {app.name!r} requires unknown app {sorted(unknown)[0]!r}"
            )
    routes = project.get("routes", "")
    if routes and (not isinstance(routes, str) or not _valid_target(routes)):
        raise ProjectError("project.routes must be a module:attribute target")
    manifest = ProjectManifest(manifest_path.resolve(), application, apps, routes)
    manifest.ordered_apps()
    return manifest


def manifest_declaring(module: str, file: str | None) -> ProjectManifest | None:
    """The manifest whose ``project.application`` is in ``module``, found from the
    directory of ``file`` upwards, else ``None``.

    Only a manifest that names this module is parsed in full, so an unrelated
    ``Hinoki()`` next to a project (a test, a script) never reads its routes.
    """
    path = find_manifest_or_none(Path(file).parent) if file else None
    if path is None or _declared_module(path) != module:
        return None
    return load_manifest(path)


def _declared_module(path: Path) -> str | None:
    """The module of ``project.application``, read without validating the rest."""
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    target = project.get("application") if isinstance(project, dict) else None
    return target.partition(":")[0] if isinstance(target, str) else None


def _valid_target(value: str) -> bool:
    module, separator, attribute = value.partition(":")
    return bool(
        separator
        and _MODULE.fullmatch(module)
        and _MODULE.fullmatch(attribute)
        and ":" not in attribute
    )


def _parse_app(value: object) -> ProjectApp:
    if not isinstance(value, dict):
        raise ProjectError("each app must be an [[apps]] table")
    name = value.get("name")
    models = value.get("models")
    migrations = value.get("migrations")
    requires = value.get("requires", [])
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise ProjectError("app names must be valid lower-case Python package names")
    if not isinstance(models, str) or not _MODULE.fullmatch(models):
        raise ProjectError(f"app {name!r} models must be a dotted module name")
    if not isinstance(migrations, str) or not _MODULE.fullmatch(migrations):
        raise ProjectError(f"app {name!r} migrations must be a dotted package name")
    if not isinstance(requires, list) or any(
        not isinstance(item, str) for item in requires
    ):
        raise ProjectError(f"app {name!r} requires must be a list of app names")
    if len(set(requires)) != len(requires):
        raise ProjectError(f"app {name!r} repeats a dependency")
    routes, prefix = _parse_app_routes(name, value)
    return ProjectApp(name, models, migrations, tuple(requires), routes, prefix)


def _parse_app_routes(name: str, value: dict[str, object]) -> tuple[str, str]:
    routes = value.get("routes", "")
    prefix = value.get("prefix", "")
    if routes and (not isinstance(routes, str) or not _valid_target(routes)):
        raise ProjectError(f"app {name!r} routes must be a module:attribute target")
    if not isinstance(prefix, str) or (
        prefix and (not prefix.startswith("/") or prefix.endswith("/"))
    ):
        raise ProjectError(
            f"app {name!r} prefix must start with '/' and not end with one"
        )
    return str(routes), prefix


def add_project_root(manifest: ProjectManifest) -> None:
    """Make the manifest's project root importable for console-script calls."""
    root = str(manifest.root)
    if root not in sys.path:
        sys.path.insert(0, root)
