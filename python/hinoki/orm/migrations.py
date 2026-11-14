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


def quote(name: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise MigrationError(f"invalid identifier {name!r}")
    return f'"{name}"'


def models_of(module_name: str) -> list[type[Model]]:
    """Models defined in ``module_name``, in definition order."""
    add_project_to_import_path()
    module = import_module(module_name)
    found = [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, Model) and member.__module__ == module_name
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
    sql = TYPES.get(kind)
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
        if meta.on_delete == "set_null" and not nullable:
            raise MigrationError(f"{model.__name__}.{name}: set_null needs `| None`")
    return {
        "type": sql,
        "nullable": nullable,
        "primary_key": meta.primary_key,
        "unique": meta.unique,
        "references": references,
    }


def table_state(model: type[Model]) -> State:
    hints = get_type_hints(model)
    columns: State = {}
    for item in dataclasses.fields(model):  # type: ignore[arg-type]
        meta: FieldMeta = item.metadata.get(MARK, FieldMeta())
        columns[item.name] = column_state(model, item.name, hints[item.name], meta)
    return {
        "columns": columns,
        "unique": [list(group) for group in model.__unique__],
        "indexes": [list(group) for group in model.__indexes__],
    }


def family(sql_type: str) -> str:
    """``varchar(80)`` and ``text`` hold the same values; compare by family."""
    name = sql_type.split("(", maxsplit=1)[0]
    return "text" if name == "varchar" else name


def check_references(state: State) -> None:
    for table, spec in state["tables"].items():
        for name, column in spec["columns"].items():
            target = column["references"]
            if target is None:
                continue
            remote = state["tables"].get(target["table"], {"columns": {}})
            wanted = remote["columns"].get(target["column"])
            if wanted is None:
                raise MigrationError(f"{table}.{name} references an unknown table")
            if family(wanted["type"]) != family(column["type"]):
                raise MigrationError(
                    f"{table}.{name} is {column['type']} but "
                    f"{target['table']}.{target['column']} is {wanted['type']}"
                )


def state_of(models: list[type[Model]]) -> State:
    tables = {m.__table__: table_state(m) for m in models}
    state: State = {"tables": tables}
    check_references(state)
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


def column_sql(name: str, column: State) -> str:
    parts = [quote(name), column["type"]]
    if column["primary_key"]:
        parts.append("PRIMARY KEY")
    elif not column["nullable"]:
        parts.append("NOT NULL")
    if column["unique"] and not column["primary_key"]:
        parts.append("UNIQUE")
    target = column["references"]
    if target:
        rule = ON_DELETE[target["on_delete"]]
        parts.append(
            f"REFERENCES {quote(target['table'])} ({quote(target['column'])}) "
            f"ON DELETE {rule}"
        )
    return " ".join(parts)


def create_table(name: str, spec: State) -> Operation:
    lines = [f"    {column_sql(c, s)}" for c, s in spec["columns"].items()]
    lines.extend(
        f"    UNIQUE ({', '.join(quote(c) for c in group)})"
        for group in spec.get("unique", [])
    )
    text = ",\n".join(lines)
    statement = f"CREATE TABLE {quote(name)} (\n{text}\n);"
    for columns in spec.get("indexes", []):
        statement += "\n" + create_index_sql(name, columns)
    return Operation(f"create table {name}", statement)


def index_name(table: str, columns: list[str]) -> str:
    """``ix_<table>_<columns>``, shortened with a hash past PostgreSQL's 63 bytes."""
    name = "ix_" + table + "_" + "_".join(columns)
    if len(name) <= MAX_IDENTIFIER:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]  # noqa: S324 - a name, not security
    return name[: MAX_IDENTIFIER - 9] + "_" + digest


def create_index_sql(table: str, columns: list[str]) -> str:
    cols = ", ".join(quote(c) for c in columns)
    name = quote(index_name(table, columns))
    return f"CREATE INDEX {name} ON {quote(table)} ({cols});"


def alter_column(table: str, name: str, old: State, new: State) -> list[Operation]:
    for key in ("primary_key", "unique", "references"):
        if old[key] != new[key]:
            raise MigrationError(
                f"{table}.{name}: changing {key} is not planned automatically; "
                "write that migration by hand"
            )
    head = f"ALTER TABLE {quote(table)} ALTER COLUMN {quote(name)}"
    operations = []
    if old["type"] != new["type"]:
        operations.append(
            Operation(
                f"change {table}.{name} to {new['type']}",
                f"{head} TYPE {new['type']} USING {quote(name)}::{new['type']};",
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


def add_column(table: str, name: str, column: State) -> Operation:
    statement = f"ALTER TABLE {quote(table)} ADD COLUMN {column_sql(name, column)};"
    risk = "" if column["nullable"] else NOT_NULL_RISK
    return Operation(f"add {table}.{name}", statement, risk)


def drop_column(table: str, name: str) -> Operation:
    return Operation(
        f"drop {table}.{name}",
        f"ALTER TABLE {quote(table)} DROP COLUMN {quote(name)};",
        "drops the column and its data",
    )


def index_changes(table: str, old: State, new: State) -> list[Operation]:
    before = [list(c) for c in old.get("indexes", [])]
    after = [list(c) for c in new.get("indexes", [])]
    operations = [
        Operation(
            f"index {table} ({', '.join(columns)})", create_index_sql(table, columns)
        )
        for columns in after
        if columns not in before
    ]
    operations.extend(
        Operation(
            f"drop index {table} ({', '.join(columns)})",
            f"DROP INDEX {quote(index_name(table, columns))};",
        )
        for columns in before
        if columns not in after
    )
    return operations


def table_changes(table: str, old: State, new: State) -> list[Operation]:
    operations: list[Operation] = []
    if old.get("unique", []) != new.get("unique", []):
        raise MigrationError(
            f"{table}: changing composite unique constraints is not planned "
            "automatically; write that migration by hand"
        )
    operations.extend(index_changes(table, old, new))
    for name, column in new["columns"].items():
        if name not in old["columns"]:
            operations.append(add_column(table, name, column))
        elif old["columns"][name] != column:
            operations.extend(alter_column(table, name, old["columns"][name], column))
    operations.extend(
        drop_column(table, name)
        for name in old["columns"]
        if name not in new["columns"]
    )
    return operations


def diff(old: State, new: State) -> list[Operation]:
    operations = [
        create_table(name, new["tables"][name])
        for name in creation_order(new["tables"])
        if name not in old["tables"]
    ]
    for name, spec in new["tables"].items():
        if name in old["tables"]:
            operations.extend(table_changes(name, old["tables"][name], spec))
    operations.extend(
        Operation(
            f"drop table {name}",
            f"DROP TABLE {quote(name)};",
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
    name: str,
    *,
    allow_destructive: bool = False,
    write: bool = True,
) -> Plan:
    """Operations needed to bring the recorded state to ``models``.

    Writes the SQL and state files unless ``write`` is false or nothing
    changed. Destructive operations raise ``DestructiveMigration`` (and write
    nothing) unless ``allow_destructive``.
    """
    if not NAME.fullmatch(name):
        raise MigrationError("migration names use lowercase letters, digits and -")
    new = state_of(models)
    operations = diff(latest_state(directory), new)
    risky = [f"{o.description}: {o.destructive}" for o in operations if o.destructive]
    if risky and not allow_destructive:
        raise DestructiveMigration(
            "destructive operations need --allow-destructive:\n  " + "\n  ".join(risky)
        )
    if not operations or not write:
        return Plan(tuple(operations), new)
    files = migration_files(directory)
    number = int(FILE.fullmatch(files[-1].name).group(1)) + 1 if files else 1  # type: ignore[union-attr]
    title = f"{number:04d}-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{title}.sql"
    path.write_text(render(title, operations), encoding="utf-8")
    path.with_suffix(".state.json").write_text(
        json.dumps(new, indent=2) + "\n", encoding="utf-8"
    )
    return Plan(tuple(operations), new, path)


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
