"""Explicit project and modular-app registry in ``hinoki.toml``."""

from __future__ import annotations

import dataclasses
import importlib
import os
import re
import sys
import tomllib
from pathlib import Path
from types import FrameType
from typing import cast

from hinoki.events import Handlers
from hinoki.http.middleware import Middleware, check_middleware
from hinoki.orm import Model
from hinoki.orm.migrations import models_of
from hinoki.orm.model import MARK, FieldMeta
from hinoki.orm.naming import MIGRATION_LEDGER, table_names_conflict
from hinoki.routing import Includable
from hinoki.rules import Rule, RuleSet
from hinoki.scaffold import NAME, RESERVED_NAMES
from hinoki.terminal import shown_path

_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectApp:
    """One app's optional persistence modules, event handlers and app dependencies."""

    name: str
    models: str = ""
    migrations: str = ""
    requires: tuple[str, ...] = ()
    events: str = ""  # ``module:attribute`` of the app's ``Handlers``

    @property
    def has_persistence(self) -> bool:
        """Whether this app declares models and migrations."""
        return bool(self.models and self.migrations)


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectManifest:
    """Validated project metadata loaded from a local TOML manifest."""

    path: Path
    application: str
    apps: tuple[ProjectApp, ...]
    routes: str = ""  # ``module:attribute`` of the project's own routes
    middleware: tuple[str, ...] = ()  # ``module:attribute`` targets, outermost first
    rules: tuple[str, ...] = ()  # ``module:attribute`` targets, each a ``RuleSet``

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

    def persistent_apps(self, selected: str | None = None) -> tuple[ProjectApp, ...]:
        """Order the full app graph first, then keep only apps with persistence."""
        return tuple(app for app in self.ordered_apps(selected) if app.has_persistence)

    def app(self, name: str | None = None) -> ProjectApp:
        """Resolve one app, requiring a name when the project is modular."""
        if name is None:
            if not self.apps:
                raise ProjectError(
                    "no apps are registered; create one with `hinoki app create <name>`"
                )
            if len(self.apps) != 1:
                raise ProjectError("pass --app because this project has multiple apps")
            return self.apps[0]
        found = next((app for app in self.apps if app.name == name), None)
        if found is None:
            raise ProjectError(f"unknown app {name!r} in {self.path.name}")
        return found

    def migrations_path(self, app: ProjectApp) -> Path:
        """Resolve an app's migration package only when a CLI command needs it."""
        if not app.has_persistence:
            raise ProjectError(f"app {app.name!r} has no models or migrations")
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
        for app in self.persistent_apps():
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
        """The project's ``[project] routes`` list, or ``[]`` when omitted.

        App exposure is declared explicitly in the project's ``routes.py`` via
        ``Mount(...)``. Importing the route module is the only effect.
        """
        if not self.routes:
            return []
        add_project_root(self)
        return self._route_list(self.routes, "project")

    def event_handlers(self) -> list[Handlers]:
        """Each app's ``Handlers`` in dependency order; an app without ``events``
        adds nothing. Importing the event modules is the only effect."""
        add_project_root(self)
        return [self._handlers(app) for app in self.ordered_apps() if app.events]

    def _handlers(self, app: ProjectApp) -> Handlers:
        module_name, _, attribute = app.events.partition(":")
        try:
            handlers = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as error:
            raise ProjectError(
                f"cannot load the events of app {app.name!r} ({app.events}): {error}"
            ) from None
        if not isinstance(handlers, Handlers):
            raise ProjectError(
                f"the events of app {app.name!r} ({app.events}) must be a "
                "hinoki.events.Handlers"
            )
        return handlers

    def middleware_list(self) -> list[Middleware]:
        """The project's ``middleware`` targets in listed order, flattened: the
        first listed runs outermost. A target is one Middleware or a list or
        tuple of them. Importing the modules is the only effect."""
        add_project_root(self)
        found: list[Middleware] = []
        for target in self.middleware:
            item = self._load_target(target, "middleware")
            items = item if isinstance(item, (list, tuple)) else (item,)
            try:
                found.extend(check_middleware(items))
            except TypeError as error:
                raise ProjectError(f"the middleware {target}: {error}") from None
        return found

    def rule_set(self) -> RuleSet:
        """The project's ``rules`` targets merged, in listed order, into one
        ``RuleSet``. The same rule (same id and summary) listed by two targets
        is an error, never an override. Importing the modules is the only
        effect."""
        add_project_root(self)
        owners: dict[tuple[str, str], str] = {}
        merged: list[Rule] = []
        for target in self.rules:
            found = self._load_target(target, "rules")
            if not isinstance(found, RuleSet):
                raise ProjectError(f"the rules {target} must be a hinoki.rules.RuleSet")
            keys = [(rule.id, rule.summary()) for rule in found.rules]
            for key in keys:
                if key in owners:
                    raise ProjectError(
                        f"rule {key[0]!r} ({key[1]}) is listed by both "
                        f"{owners[key]} and {target}"
                    )
            owners.update(dict.fromkeys(keys, target))
            merged.extend(found.rules)
        return RuleSet(*merged)

    @staticmethod
    def _load_target(target: str, what: str) -> object:
        module_name, _, attribute = target.partition(":")
        try:
            return getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as error:
            raise ProjectError(f"cannot load the {what} {target}: {error}") from None

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
        if not app.has_persistence:
            raise ProjectError(f"app {app.name!r} has no models or migrations")
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
        for app in self.persistent_apps():
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
    if not isinstance(routes, str) or (routes and not _valid_target(routes)):
        raise ProjectError("project.routes must be a module:attribute target")
    middleware = _parse_target_list(project, "middleware")
    rules = _parse_target_list(project, "rules")
    manifest = ProjectManifest(
        manifest_path.resolve(), application, apps, routes, middleware, rules
    )
    manifest.ordered_apps()
    return manifest


def _parse_target_list(project: dict[str, object], key: str) -> tuple[str, ...]:
    found = project.get(key, [])
    if (
        not isinstance(found, list)
        or any(not isinstance(item, str) for item in found)
        or not all(_valid_target(item) for item in found)
    ):
        raise ProjectError(f"project.{key} must be a list of module:attribute targets")
    return tuple(found)


def manifest_events(start: Path | None = None) -> list[Handlers]:
    """The ``Handlers`` each app of ``hinoki.toml`` declares, in dependency order,
    for ``Hinoki(events=manifest_events())``. The manifest is found as for
    ``manifest_routes``; outside a project this raises ``ProjectError``."""
    directory = start if start is not None else _caller_directory(sys._getframe(1))
    return _manifest_from(directory).event_handlers()


def manifest_middleware(start: Path | None = None) -> list[Middleware]:
    """The ``[project] middleware`` list of ``hinoki.toml``, flattened in listed
    order (the first listed runs outermost), for
    ``Hinoki(middleware=manifest_middleware())``. The manifest is found as for
    ``manifest_routes``; outside a project this raises ``ProjectError``."""
    directory = start if start is not None else _caller_directory(sys._getframe(1))
    return _manifest_from(directory).middleware_list()


def manifest_rules(start: Path | None = None) -> RuleSet:
    """The ``[project] rules`` list of ``hinoki.toml`` merged, in listed order,
    into one ``RuleSet`` for ``Hinoki(rules=manifest_rules())``. The manifest is
    found as for ``manifest_routes``; outside a project this raises
    ``ProjectError``."""
    directory = start if start is not None else _caller_directory(sys._getframe(1))
    return _manifest_from(directory).rule_set()


def manifest_routes(start: Path | None = None) -> list[Includable]:
    """The routes ``[project] routes`` declares in ``hinoki.toml`` (or ``[]``
    when omitted).

    The manifest is found from ``start``, by default the directory of the calling
    file (the working directory when the caller has no file), upwards. Outside a
    project this raises ``ProjectError``.
    """
    directory = start if start is not None else _caller_directory(sys._getframe(1))
    return _manifest_from(directory).route_items()


def _manifest_from(directory: Path | None) -> ProjectManifest:
    written = (directory if directory is not None else Path.cwd()).absolute()
    resolved = written.resolve()
    path = find_manifest_or_none(resolved) or _manifest_along(written)
    if path is None:
        searched = shown_path(written)
        if resolved != written:
            searched += f" (resolved: {shown_path(resolved)})"
        raise ProjectError(f"no hinoki.toml found in {searched} or above it")
    return load_manifest(path)


def _manifest_along(directory: Path) -> Path | None:
    """The manifest above ``directory`` as written, symlinks unresolved: a
    package linked into a project is still found from inside it."""
    for candidate in (directory, *directory.parents):
        if (candidate / "hinoki.toml").is_file():
            return candidate / "hinoki.toml"
    return None


def _caller_directory(frame: FrameType) -> Path | None:
    file = frame.f_globals.get("__file__")
    return Path(file).parent if isinstance(file, str) else None


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
    requires = value.get("requires", [])
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED_NAMES:
        raise ProjectError("app names must be valid lower-case Python package names")
    _reject_legacy_app_routing(name, value)
    models, migrations = _parse_persistence(name, value)
    if not isinstance(requires, list) or any(
        not isinstance(item, str) for item in requires
    ):
        raise ProjectError(f"app {name!r} requires must be a list of app names")
    if len(set(requires)) != len(requires):
        raise ProjectError(f"app {name!r} repeats a dependency")
    events = _parse_target(name, value, "events")
    return ProjectApp(name, models, migrations, tuple(requires), events)


def _parse_persistence(name: str, value: dict[str, object]) -> tuple[str, str]:
    models = value.get("models", "")
    migrations = value.get("migrations", "")
    if not isinstance(models, str) or (models and not _MODULE.fullmatch(models)):
        raise ProjectError(f"app {name!r} models must be a dotted module name")
    if not isinstance(migrations, str) or (
        migrations and not _MODULE.fullmatch(migrations)
    ):
        raise ProjectError(f"app {name!r} migrations must be a dotted package name")
    if bool(models) != bool(migrations):
        raise ProjectError(
            f"app {name!r} must declare both 'models' and 'migrations', or neither"
        )
    return models, migrations


def _parse_target(name: str, value: dict[str, object], key: str) -> str:
    target = value.get(key, "")
    if not isinstance(target, str) or (target and not _valid_target(target)):
        raise ProjectError(f"app {name!r} {key} must be a module:attribute target")
    return target


def _reject_legacy_app_routing(name: str, value: dict[str, object]) -> None:
    keys = [key for key in ("routes", "prefix") if key in value]
    if not keys:
        return
    removed = " and ".join(repr(key) for key in keys)
    raw_routes = value.get("routes")
    raw_prefix = value.get("prefix")
    mount_prefix = (
        raw_prefix
        if isinstance(raw_prefix, str)
        and raw_prefix.startswith("/")
        and not raw_prefix.endswith("/")
        else f"/{name}"
    )
    if isinstance(raw_routes, str) and raw_routes and _valid_target(raw_routes):
        module_name, _, attribute = raw_routes.partition(":")
        alias = f"{name}_routes"
        raise ProjectError(
            f"app {name!r} uses removed [[apps]] key(s) {removed}; "
            "remove them from hinoki.toml and mount the app in your project's "
            f"routes.py: from {module_name} import {attribute} as {alias}; "
            f'Mount("{mount_prefix}", {alias})'
        )
    if "routes" in value:
        raise ProjectError(
            f"app {name!r} uses removed [[apps]] key(s) {removed}; "
            "remove them from hinoki.toml and declare an explicit "
            f'Mount("{mount_prefix}", ...) in your project\'s routes.py'
        )
    raise ProjectError(
        f"app {name!r} uses removed [[apps]] key {removed}; "
        "remove it from hinoki.toml and choose the mount prefix with "
        f'Mount("{mount_prefix}", ...) in your project\'s routes.py'
    )


def add_project_root(manifest: ProjectManifest) -> None:
    """Make the manifest's project root importable for console-script calls."""
    root = str(manifest.root)
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_target(target: str | None) -> str:
    """Return an explicit ``TARGET``, or the manifest's ``project.application``.

    The default also puts the project root on this process's ``sys.path``. An
    explicit target is returned unchanged and never reads the manifest.
    """
    if target is not None:
        return target
    path = find_manifest_or_none()
    if path is None:
        raise ProjectError("pass TARGET outside a project with hinoki.toml")
    manifest = load_manifest(path)
    add_project_root(manifest)
    return manifest.application
