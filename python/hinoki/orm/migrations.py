"""Schema state, migration planning and the on-disk migration format.

Migrations are plain SQL files, ``NNNN-name.sql``, each with a
``NNNN-name.state.json`` holding the schema after it. Planning compares the
models with the newest state file and writes the difference; nothing here
touches a database (see ``hinoki.orm.migrator`` for applying). A statement ends at
a line that ends with ``;``. Operations that lose data or may fail on existing
rows are reported as destructive and only written when explicitly allowed.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import types
from importlib import import_module
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from hinoki import core, jsontext
from hinoki.errors import DestructiveMigration, MigrationError
from hinoki.orm import Model
from hinoki.orm.columntypes import sql_type
from hinoki.orm.links import check_resolved
from hinoki.orm.model import MARK, FieldMeta
from hinoki.orm.naming import MIGRATION_LEDGER, table_names_conflict
from hinoki.target import add_project_to_import_path

MAX_IDENTIFIER = 63  # PostgreSQL truncates longer names
UNIQUE_KEYS: tuple[tuple[str, Any], ...] = (("unique", []), ("unique_names", {}))
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
FILE = re.compile(r"(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.sql")
SQLITE_STATE_TYPES = {
    "uuid": "UUID",
    "bigint": "integer",
    "double precision": "real",
    "bytea": "blob",
    "numeric": "text",
}
MYSQL_STATE_TYPES = {
    "numeric": "decimal(65,30)",
    "uuid": "binary(16)",
    "double precision": "double",
    "timestamptz": "datetime(6)",
    "bytea": "longblob",
    "jsonb": "json",
}
LEGACY_STATE_TYPES = {
    "uuid": "uuid",
    "integer": "bigint",
    "real": "double precision",
    "blob": "bytea",
    "binary(16)": "uuid",
    "double": "double precision",
    "datetime(6)": "timestamptz",
    "longblob": "bytea",
    "json": "jsonb",
}
EMPTY: dict[str, Any] = {"tables": {}}
STATE_FORMAT_VERSION = 1

State = dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class Operation:
    description: str
    sql: str
    destructive: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Plan:
    operations: tuple[Operation, ...]
    state: State
    path: Path | None = None


def _to_ops(raw: list[tuple[str, str, str]]) -> list[Operation]:
    return [Operation(desc, sql, risk) for desc, sql, risk in raw]


def quote(name: str, dialect: str = "postgres") -> str:
    try:
        return core.migration_quote(name, dialect)
    except ValueError as error:
        raise MigrationError(str(error)) from None


def models_of(module_name: str) -> list[type[Model]]:
    """Models defined in ``module_name``, in definition order."""
    add_project_to_import_path()
    module = import_module(module_name)
    found = [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, Model)
        and member.__module__ == module_name
        and not member.__abstract__  # no table: nothing to migrate
    ]
    return sorted(found, key=lambda m: inspect.getsourcelines(m)[1])


def base_type(model: type[Model], name: str, hint: Any) -> tuple[Any, bool]:
    """The non-optional type of a column annotation and whether it is optional."""
    args = get_args(hint)
    if get_origin(hint) not in (Union, types.UnionType):
        return get_origin(hint) or hint, False
    rest = [a for a in args if a is not type(None)]
    if len(rest) != 1:
        raise MigrationError(f"{model.__name__}.{name}: unions are not supported")
    return get_origin(rest[0]) or rest[0], len(rest) != len(args)


def column_state(model: type[Model], name: str, hint: Any, meta: FieldMeta) -> State:
    kind, nullable = base_type(model, name, hint)
    sql = sql_type(kind)
    if sql is None:
        label = getattr(kind, "__name__", kind)
        raise MigrationError(f"{model.__name__}.{name}: no SQL type for {label}")
    max_length = meta.max_length or model.__field_types__[name].info.max_length
    if kind is str and max_length:
        sql = f"varchar({max_length})"
    references = None
    if meta.references is not None:
        references = {
            "table": meta.references.__table__,
            "column": meta.references.__primary_key__,
            "on_delete": meta.on_delete,
        }
        # Keep this check for string/forward annotations which were unresolved
        # when the model class was declared; get_type_hints() above resolves them.
        if meta.on_delete == "set_null" and not nullable:
            raise MigrationError(f"{model.__name__}.{name}: set_null needs `| None`")
    return {
        "type": sql,
        "nullable": nullable,
        "primary_key": meta.primary_key,
        "unique": meta.unique,
        "references": references,
    }


def table_state(model: type[Model], dialect: str = "postgres") -> State:  # noqa: ARG001 - retained for compatibility
    """Return backend-neutral column types for the migration state file."""
    hints = get_type_hints(model)
    columns: State = {}
    for item in dataclasses.fields(model):  # type: ignore[arg-type]
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        columns[item.name] = column_state(model, item.name, hints[item.name], meta)
    state: State = {
        "columns": columns,
        "unique": [list(group) for group in model.__unique__],
        "indexes": [list(group) for group in model.__indexes__],
    }
    if model.__unique_names__:
        state["unique_names"] = {
            name: list(group) for group, name in model.__unique_names__.items()
        }
    return state


def check_constraint_names(tables: State) -> None:
    """Named unique constraints must not reuse a name the database already
    holds: PostgreSQL keeps tables, indexes and unique constraints in one
    namespace per schema, and MySQL and MariaDB compare names without case."""
    try:
        core.migration_check_constraint_names(jsontext.dumps(tables))
    except ValueError as error:
        raise MigrationError(str(error)) from None


def family(sql_type: str) -> str:
    """``varchar(80)`` and ``text`` hold the same values; compare by family."""
    name = sql_type.split("(", maxsplit=1)[0]
    return "text" if name == "varchar" else name


def check_references(state: State, external_tables: State | None = None) -> None:
    ext = None if external_tables is None else jsontext.dumps(external_tables)
    try:
        core.migration_check_references(jsontext.dumps(state), ext)
    except ValueError as error:
        raise MigrationError(str(error)) from None


def state_of(
    models: list[type[Model]],
    dialect: str = "postgres",  # noqa: ARG001 - state types are backend-neutral
    *,
    reference_models: list[type[Model]] | None = None,
) -> State:
    check_resolved()
    owned = _unique_model_classes(models)
    references = _unique_model_classes(reference_models or [])
    _reject_unusable_tables([*owned, *references])
    tables: dict[str, Any] = {}
    owners: list[type[Model]] = []
    for model in owned:
        _check_table_owner(model, owners)
        owners.append(model)
        tables[model.__table__] = table_state(model)
    state: State = {"tables": tables}
    external: dict[str, Any] = {}
    for model in references:
        if model in owned:
            continue
        _check_table_owner(model, owners)
        owners.append(model)
        external[model.__table__] = table_state(model)
    check_references(state, {"tables": external})
    check_constraint_names({**external, **tables})
    return state


def _unique_model_classes(models: list[type[Model]]) -> list[type[Model]]:
    """The models once each, every generated bridge right after its owner so the
    tables it references come first."""
    unique: list[type[Model]] = []
    seen: set[int] = set()
    for owner in models:
        for model in (owner, *owner.__bridges__):
            if id(model) not in seen:
                seen.add(id(model))
                unique.append(model)
    return unique


def _reject_unusable_tables(models: list[type[Model]]) -> None:
    for model in models:
        if model.__table_problem__:
            raise MigrationError(model.__table_problem__)
        if model.__table__.casefold() == MIGRATION_LEDGER:
            raise MigrationError(
                f"{model.__name__} claims Hinoki's migration ledger table "
                f"{MIGRATION_LEDGER!r}; choose a distinct application table"
            )


def _check_table_owner(model: type[Model], owners: list[type[Model]]) -> None:
    for previous in owners:
        if table_names_conflict(previous, model):
            raise MigrationError(
                f"models {previous.__module__}.{previous.__qualname__} and "
                f"{model.__module__}.{model.__qualname__} both claim or ambiguously "
                f"resolve to table {model.__table__!r}; choose distinct table= values"
            )


def creation_order(tables: State) -> list[str]:
    """Tables ordered so every referenced table comes first."""
    try:
        return core.migration_creation_order(jsontext.dumps(tables))
    except ValueError as error:
        raise MigrationError(str(error)) from None


def dialect_type(state_type: str, dialect: str) -> str:
    """Render a logical migration-state type for the selected SQL backend."""
    return core.migration_dialect_type(state_type, dialect)


def create_table_sql(name: str, spec: State, dialect: str = "postgres") -> str:
    try:
        return core.migration_create_table_sql(name, jsontext.dumps(spec), dialect)
    except ValueError as error:
        raise MigrationError(str(error)) from None


def index_name(table: str, columns: list[str], dialect: str = "postgres") -> str:
    """``ix_<table>_<columns>_<digest>``, cut to the backend's identifier limit."""
    return core.migration_index_name(table, columns, dialect)


def foreign_key_sql(table: str, column: str, state: State, dialect: str) -> str:
    """Render a named table-level FK, required by MySQL-family servers."""
    try:
        return core.migration_foreign_key_sql(
            table, column, jsontext.dumps(state), dialect
        )
    except ValueError as error:
        raise MigrationError(str(error)) from None


def foreign_key_name(table: str, column: str, dialect: str) -> str:
    """Stable FK names, shortened to the backend's identifier limit."""
    return core.migration_foreign_key_name(table, column, dialect)


def alter_column(
    table: str, name: str, old: State, new: State, dialect: str = "postgres"
) -> list[Operation]:
    try:
        raw = core.migration_alter_column(
            table, name, jsontext.dumps(old), jsontext.dumps(new), dialect
        )
    except ValueError as error:
        raise MigrationError(str(error)) from None
    return _to_ops(raw)


def _sqlite_rebuild_warnings(old: State, new: State) -> list[str]:
    return core.migration_sqlite_rebuild_warnings(
        jsontext.dumps(old), jsontext.dumps(new)
    )


def table_changes(table: str, old: State, new: State, dialect: str) -> list[Operation]:
    try:
        raw = core.migration_table_changes(
            table, jsontext.dumps(old), jsontext.dumps(new), dialect
        )
    except ValueError as error:
        raise MigrationError(str(error)) from None
    return _to_ops(raw)


def diff(old: State, new: State, dialect: str = "postgres") -> list[Operation]:
    try:
        raw = core.migration_diff(jsontext.dumps(old), jsontext.dumps(new), dialect)
    except ValueError as error:
        raise MigrationError(str(error)) from None
    return _to_ops(raw)


def migration_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if FILE.fullmatch(p.name))


def latest_state(directory: Path) -> State:
    files = migration_files(directory)
    if not files:
        return EMPTY
    state: State = EMPTY
    for migration in files:
        path = migration.with_suffix(".state.json")
        if not path.is_file():
            raise MigrationError(
                f"{path.name} is missing; restore it from version control"
            )
        try:
            document = jsontext.loads(path.read_bytes())
        except ValueError as error:
            raise MigrationError(f"{path.name} is not valid JSON") from error
        state = validate_migration_state(document, path.name)
    return state


def validate_migration_state(state: object, filename: str) -> State:
    """Validate and normalize a versioned or legacy migration state document."""
    try:
        return core.migration_validate_state(state, filename)
    except ValueError as error:
        raise MigrationError(str(error)) from None


def render(title: str, operations: list[Operation]) -> str:
    return core.migration_render(
        title, [(o.description, o.sql, o.destructive) for o in operations]
    )


def plan(
    models: list[type[Model]],
    directory: Path,
    name: str | None = None,
    *,
    allow_destructive: bool = False,
    write: bool = True,
    dialect: str = "postgres",
    reference_models: list[type[Model]] | None = None,
) -> Plan:
    """Operations needed to bring the recorded state to ``models``.

    Writes the SQL and state files unless ``write`` is false or nothing
    changed. Destructive operations raise ``DestructiveMigration`` (and write
    nothing) unless ``allow_destructive``.
    """
    if name is not None and not NAME.fullmatch(name):
        raise MigrationError("migration names use lowercase letters, digits and -")
    if dialect not in {"postgres", "sqlite", "mysql", "mariadb"}:
        raise MigrationError(f"unsupported migration dialect {dialect!r}")
    new = state_of(models, dialect, reference_models=reference_models)
    operations = diff(latest_state(directory), new, dialect)
    risky = [f"{o.description}: {o.destructive}" for o in operations if o.destructive]
    if risky and not allow_destructive:
        raise DestructiveMigration(
            "destructive operations need --allow-destructive:\n  " + "\n  ".join(risky)
        )
    if not operations or not write:
        return Plan(tuple(operations), new)
    migration_name = name or automatic_migration_name(operations)
    files = migration_files(directory)
    number = int(FILE.fullmatch(files[-1].name).group(1)) + 1 if files else 1  # type: ignore[union-attr]
    title = f"{number:04d}-{migration_name}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{title}.sql"
    path.write_text(render(title, operations), encoding="utf-8")
    state_document = {"format_version": STATE_FORMAT_VERSION, **new}
    path.with_suffix(".state.json").write_text(
        jsontext.dumps(state_document, indent=2) + "\n", encoding="utf-8"
    )
    return Plan(tuple(operations), new, path)


def automatic_migration_name(operations: list[Operation]) -> str:
    """Derive a stable file-name slug from the first planned schema operation."""
    return core.migration_auto_name(operations[0].description)


def statements(text: str) -> list[str]:
    """Split a migration file: comments and blanks are skipped and a statement
    ends at a line ending with ``;``."""
    try:
        return core.migration_statements(text)
    except ValueError as error:
        raise MigrationError(str(error)) from None
