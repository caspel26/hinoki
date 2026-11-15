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
import hashlib
import inspect
import json
import re
import types
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints
from uuid import UUID

from hinoki.errors import DestructiveMigration, MigrationError
from hinoki.orm import MARK, FieldMeta, Model
from hinoki.orm.relations import ON_DELETE
from hinoki.target import add_project_to_import_path

MAX_IDENTIFIER = 63  # PostgreSQL truncates longer names

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
FILE = re.compile(r"(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.sql")
TYPES: dict[Any, str] = {
    UUID: "uuid",
    str: "text",
    int: "bigint",
    float: "double precision",
    bool: "boolean",
    datetime: "timestamptz",
    bytes: "bytea",
    dict: "jsonb",
    list: "jsonb",
}
SQLITE_TYPES: dict[Any, str] = {
    UUID: "UUID",
    str: "text",
    int: "integer",
    float: "real",
    bool: "boolean",
    datetime: "timestamptz",
    bytes: "blob",
    dict: "jsonb",
    list: "jsonb",
}
MYSQL_TYPES: dict[Any, str] = {
    UUID: "binary(16)",
    str: "text",
    int: "bigint",
    float: "double",
    bool: "boolean",
    datetime: "datetime(6)",
    bytes: "longblob",
    dict: "json",
    list: "json",
}
EMPTY: dict[str, Any] = {"tables": {}}

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


def quote(name: str, dialect: str = "postgres") -> str:
    limit = 64 if dialect in {"mysql", "mariadb"} else MAX_IDENTIFIER
    if not re.fullmatch(rf"[A-Za-z_][A-Za-z0-9_]{{0,{limit - 1}}}", name):
        raise MigrationError(f"invalid identifier {name!r}")
    return f"`{name}`" if dialect in {"mysql", "mariadb"} else f'"{name}"'


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


def column_state(
    model: type[Model], name: str, hint: Any, meta: FieldMeta, dialect: str
) -> State:
    kind, nullable = base_type(model, name, hint)
    types = (
        SQLITE_TYPES
        if dialect == "sqlite"
        else MYSQL_TYPES
        if dialect in {"mysql", "mariadb"}
        else TYPES
    )
    sql = types.get(kind)
    if sql is None:
        label = getattr(kind, "__name__", kind)
        raise MigrationError(f"{model.__name__}.{name}: no SQL type for {label}")
    if kind is str and meta.max_length:
        sql = f"varchar({meta.max_length})"
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


def table_state(model: type[Model], dialect: str = "postgres") -> State:
    hints = get_type_hints(model)
    columns: State = {}
    for item in dataclasses.fields(model):  # type: ignore[arg-type]
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        columns[item.name] = column_state(
            model, item.name, hints[item.name], meta, dialect
        )
    return {
        "columns": columns,
        "unique": [list(group) for group in model.__unique__],
        "indexes": [list(group) for group in model.__indexes__],
    }


def family(sql_type: str) -> str:
    """``varchar(80)`` and ``text`` hold the same values; compare by family."""
    name = sql_type.split("(", maxsplit=1)[0]
    return "text" if name == "varchar" else name


def check_references(state: State, external_tables: State | None = None) -> None:
    for table, spec in state["tables"].items():
        for name, column in spec["columns"].items():
            target = column["references"]
            if target is None:
                continue
            remote = state["tables"].get(
                target["table"],
                (external_tables or {"tables": {}})["tables"].get(
                    target["table"], {"columns": {}}
                ),
            )
            wanted = remote["columns"].get(target["column"])
            if wanted is None:
                raise MigrationError(f"{table}.{name} references an unknown table")
            if family(wanted["type"]) != family(column["type"]):
                raise MigrationError(
                    f"{table}.{name} is {column['type']} but "
                    f"{target['table']}.{target['column']} is {wanted['type']}"
                )


def state_of(
    models: list[type[Model]],
    dialect: str = "postgres",
    *,
    reference_models: list[type[Model]] | None = None,
) -> State:
    tables = {m.__table__: table_state(m, dialect) for m in models}
    state: State = {"tables": tables}
    external = {
        model.__table__: table_state(model, dialect)
        for model in reference_models or []
        if model.__table__ not in tables
    }
    check_references(state, {"tables": external})
    return state


def creation_order(tables: State) -> list[str]:
    """Tables ordered so every referenced table comes first."""
    ordered: list[str] = []
    pending = dict(tables)
    while pending:
        ready = [
            name
            for name, spec in pending.items()
            if all(
                c["references"] is None
                or c["references"]["table"] in ordered
                or c["references"]["table"] == name
                or c["references"]["table"] not in tables
                for c in spec["columns"].values()
            )
        ]
        if not ready:
            raise MigrationError("foreign keys form a cycle between tables")
        for name in ready:
            ordered.append(name)
            del pending[name]
    return ordered


def column_sql(name: str, column: State, dialect: str = "postgres") -> str:
    parts = [quote(name, dialect), column["type"]]
    if column["primary_key"]:
        parts.append("PRIMARY KEY")
    elif not column["nullable"]:
        parts.append("NOT NULL")
    if column["unique"] and not column["primary_key"]:
        parts.append("UNIQUE")
    target = column["references"]
    if target:
        rule = ON_DELETE[target["on_delete"]]
        target_table = quote(target["table"], dialect)
        target_column = quote(target["column"], dialect)
        parts.append(f"REFERENCES {target_table} ({target_column}) ON DELETE {rule}")
    return " ".join(parts)


def create_table(name: str, spec: State, dialect: str = "postgres") -> Operation:
    statement = create_table_sql(name, spec, dialect)
    for columns in spec.get("indexes", []):
        statement += "\n" + create_index_sql(name, columns, dialect)
    return Operation(f"create table {name}", statement)


def create_table_sql(name: str, spec: State, dialect: str = "postgres") -> str:
    lines = [f"    {column_sql(c, s, dialect)}" for c, s in spec["columns"].items()]
    lines.extend(
        f"    UNIQUE ({', '.join(quote(c, dialect) for c in group)})"
        for group in spec.get("unique", [])
    )
    text = ",\n".join(lines)
    suffix = " ENGINE=InnoDB" if dialect in {"mysql", "mariadb"} else ""
    return f"CREATE TABLE {quote(name, dialect)} (\n{text}\n){suffix};"


def index_name(table: str, columns: list[str], dialect: str = "postgres") -> str:
    """``ix_<table>_<columns>``, shortened with a hash past PostgreSQL's 63 bytes."""
    name = "ix_" + table + "_" + "_".join(columns)
    limit = 64 if dialect in {"mysql", "mariadb"} else MAX_IDENTIFIER
    if len(name) <= limit:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]  # noqa: S324 - a name, not security
    return name[: limit - 9] + "_" + digest


def create_index_sql(table: str, columns: list[str], dialect: str = "postgres") -> str:
    cols = ", ".join(quote(c, dialect) for c in columns)
    name = quote(index_name(table, columns, dialect), dialect)
    return f"CREATE INDEX {name} ON {quote(table, dialect)} ({cols});"


def alter_column(
    table: str, name: str, old: State, new: State, dialect: str = "postgres"
) -> list[Operation]:
    for key in ("primary_key", "unique", "references"):
        if old[key] != new[key]:
            raise MigrationError(
                f"{table}.{name}: changing {key} is not planned automatically; "
                "write that migration by hand"
            )
    if dialect in {"mysql", "mariadb"}:
        column = column_sql(name, new, dialect)
        return [
            Operation(
                f"change {table}.{name}",
                f"ALTER TABLE {quote(table, dialect)} MODIFY COLUMN {column};",
                "changing a column definition may rewrite the table or lose data",
            )
        ]
    head = f"ALTER TABLE {quote(table, dialect)} ALTER COLUMN {quote(name, dialect)}"
    operations = []
    if old["type"] != new["type"]:
        operations.append(
            Operation(
                f"change {table}.{name} to {new['type']}",
                f"{head} TYPE {new['type']} USING "
                f"{quote(name, dialect)}::{new['type']};",
                "changing a column type can rewrite the table or lose data",
            )
        )
    if old["nullable"] and not new["nullable"]:
        operations.append(
            Operation(
                f"require {table}.{name}",
                f"{head} SET NOT NULL;",
                "fails when the column contains NULL",
            )
        )
    if not old["nullable"] and new["nullable"]:
        operations.append(
            Operation(f"allow NULL in {table}.{name}", f"{head} DROP NOT NULL;")
        )
    return operations


NOT_NULL_RISK = "adding a NOT NULL column without a default fails when rows exist"


def add_column(
    table: str, name: str, column: State, dialect: str = "postgres"
) -> Operation:
    definition = column_sql(name, column, dialect)
    statement = f"ALTER TABLE {quote(table, dialect)} ADD COLUMN {definition};"
    risk = "" if column["nullable"] else NOT_NULL_RISK
    return Operation(f"add {table}.{name}", statement, risk)


def drop_column(table: str, name: str, dialect: str = "postgres") -> Operation:
    return Operation(
        f"drop {table}.{name}",
        f"ALTER TABLE {quote(table, dialect)} DROP COLUMN {quote(name, dialect)};",
        "drops the column and its data",
    )


def index_changes(
    table: str, old: State, new: State, dialect: str = "postgres"
) -> list[Operation]:
    before = [list(c) for c in old.get("indexes", [])]
    after = [list(c) for c in new.get("indexes", [])]
    operations = [
        Operation(
            f"index {table} ({', '.join(columns)})",
            create_index_sql(table, columns, dialect),
        )
        for columns in after
        if columns not in before
    ]
    operations.extend(
        Operation(
            f"drop index {table} ({', '.join(columns)})",
            (
                "DROP INDEX "
                f"{quote(index_name(table, columns, dialect), dialect)} "
                f"ON {quote(table, dialect)};"
                if dialect in {"mysql", "mariadb"}
                else "DROP INDEX "
                f"{quote(index_name(table, columns, dialect), dialect)};"
            ),
        )
        for columns in before
        if columns not in after
    )
    return operations


def _sqlite_rebuild_warnings(old: State, new: State) -> list[str]:
    destructive: list[str] = []
    for name, column in new["columns"].items():
        previous = old["columns"].get(name)
        if previous is None:
            if not column["nullable"]:
                destructive.append(f"adding {name} requires values for existing rows")
        elif previous is not None:
            destructive.extend(_sqlite_column_warnings(name, previous, column))
    destructive.extend(
        f"dropping {name} loses its data"
        for name in old["columns"]
        if name not in new["columns"]
    )
    if old.get("unique", []) != new.get("unique", []):
        destructive.append(
            "changing composite unique constraints may reject existing rows"
        )
    return destructive


def _sqlite_column_warnings(
    name: str, previous: dict[str, Any], column: dict[str, Any]
) -> list[str]:
    warnings: list[str] = []
    if previous == column:
        return warnings
    if previous["type"] != column["type"]:
        warnings.append(f"changing {name} to {column['type']} may convert values")
    if previous["nullable"] and not column["nullable"]:
        warnings.append(f"requiring {name} fails when existing rows contain NULL")
    warnings.extend(
        f"changing {key} on {name} may reject existing rows"
        for key in ("primary_key", "unique", "references")
        if previous[key] != column[key]
    )
    return warnings


def sqlite_rebuild(table: str, old: State, new: State) -> Operation:
    digest = hashlib.sha1(table.encode()).hexdigest()[:8]  # noqa: S324 - temporary table name
    temporary = f"__hinoki_{digest}"
    statements = [create_table_sql(temporary, new)]
    common = [name for name in old["columns"] if name in new["columns"]]
    destructive = _sqlite_rebuild_warnings(old, new)
    if common:
        columns = ", ".join(quote(name) for name in common)
        statements.append(
            f"INSERT INTO {quote(temporary)} ({columns}) "  # noqa: S608 - quoted identifiers
            f"SELECT {columns} FROM {quote(table)};"
        )
    statements.extend(
        [
            f"DROP TABLE {quote(table)};",
            f"ALTER TABLE {quote(temporary)} RENAME TO {quote(table)};",
        ]
    )
    statements.extend(
        create_index_sql(table, columns) for columns in new.get("indexes", [])
    )
    return Operation(
        f"rebuild table {table}",
        "\n".join(statements),
        "; ".join(destructive),
    )


def table_changes(table: str, old: State, new: State, dialect: str) -> list[Operation]:
    if dialect == "sqlite":
        if old["columns"] != new["columns"] or old.get("unique", []) != new.get(
            "unique", []
        ):
            return [sqlite_rebuild(table, old, new)]
        return index_changes(table, old, new, dialect)
    operations: list[Operation] = []
    if old.get("unique", []) != new.get("unique", []):
        raise MigrationError(
            f"{table}: changing composite unique constraints is not planned "
            "automatically; write that migration by hand"
        )
    operations.extend(index_changes(table, old, new, dialect))
    for name, column in new["columns"].items():
        if name not in old["columns"]:
            operations.append(add_column(table, name, column, dialect))
        elif old["columns"][name] != column:
            operations.extend(
                alter_column(table, name, old["columns"][name], column, dialect)
            )
    operations.extend(
        drop_column(table, name, dialect)
        for name in old["columns"]
        if name not in new["columns"]
    )
    return operations


def diff(old: State, new: State, dialect: str = "postgres") -> list[Operation]:
    operations = [
        create_table(name, new["tables"][name], dialect)
        for name in creation_order(new["tables"])
        if name not in old["tables"]
    ]
    for name, spec in new["tables"].items():
        if name in old["tables"]:
            operations.extend(table_changes(name, old["tables"][name], spec, dialect))
    operations.extend(
        Operation(
            f"drop table {name}",
            f"DROP TABLE {quote(name, dialect)};",
            "drops the table and all its rows",
        )
        for name in reversed(creation_order(old["tables"]))
        if name not in new["tables"]
    )
    return operations


def migration_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if FILE.fullmatch(p.name))


def latest_state(directory: Path) -> State:
    files = migration_files(directory)
    if not files:
        return EMPTY
    path = files[-1].with_suffix(".state.json")
    if not path.is_file():
        raise MigrationError(f"{path.name} is missing; restore it from version control")
    state: State = json.loads(path.read_text(encoding="utf-8"))
    return state


def render(title: str, operations: list[Operation]) -> str:
    lines = [
        f"-- hinoki migration {title}",
        "-- generated from the models; review it.",
        "",
    ]
    for operation in operations:
        lines.append(f"-- {operation.description}")
        if operation.destructive:
            lines.append(f"-- DESTRUCTIVE: {operation.destructive}")
        lines.extend([operation.sql, ""])
    return "\n".join(lines)


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
    path.with_suffix(".state.json").write_text(
        json.dumps(new, indent=2) + "\n", encoding="utf-8"
    )
    return Plan(tuple(operations), new, path)


def automatic_migration_name(operations: list[Operation]) -> str:
    """Derive a stable file-name slug from the first planned schema operation."""
    source = operations[0].description.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", source).strip("-")
    return slug[:80].rstrip("-") or "schema-update"


def statements(text: str) -> list[str]:
    """Split a migration file: comments and blanks are skipped and a statement
    ends at a line ending with ``;``."""
    found: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("--")):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            found.append("\n".join(current).rstrip().removesuffix(";"))
            current = []
    if current:
        raise MigrationError("the last statement does not end with ';'")
    return found
