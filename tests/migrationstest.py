"""Migration planning (no database) and applying (disposable PostgreSQL)."""

import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from hinoki.__main__ import database_dialect, main
from hinoki.db import Database, ForeignKeyViolation
from hinoki.errors import DestructiveMigration, MigrationError
from hinoki.orm import Model, OnDelete, field, foreign_key, migrator, use_database
from hinoki.orm.migrations import (
    _sqlite_rebuild_warnings,
    alter_column,
    check_references,
    creation_order,
    foreign_key_name,
    foreign_key_sql,
    models_of,
    plan,
    statements,
    table_changes,
)
from hinoki.project import load_manifest

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
MYSQL_URL = os.environ.get("HINOKI_TEST_MYSQL_URL")
MARIADB_URL = os.environ.get("HINOKI_TEST_MARIADB_URL")
needs_database = pytest.mark.skipif(
    URL is None, reason="HINOKI_TEST_DATABASE_URL is not set"
)


def model(table: str, columns: dict[str, tuple[Any, Any]]) -> type[Model]:
    """A model with ``name: (annotation, field)`` columns."""
    return type(
        table.title(),
        (Model,),
        {
            "__annotations__": {n: a for n, (a, _) in columns.items()},
            **{n: f for n, (_, f) in columns.items() if f is not None},
        },
        table=table,
    )


def owners(extra: dict[str, tuple[Any, Any]] | None = None) -> type[Model]:
    columns: dict[str, tuple[Any, Any]] = {
        "id": (int, field(primary_key=True)),
        "name": (str, field(max_length=40)),
    }
    return model("mig_owners", {**columns, **(extra or {})})


def things(owner: type[Model], **changes: tuple[Any, Any]) -> type[Model]:
    columns: dict[str, tuple[Any, Any]] = {
        "id": (UUID, field(primary_key=True)),
        "owner_id": (int, foreign_key(owner, on_delete=OnDelete.CASCADE)),
        "tags": (dict[str, Any], None),
        "seen": (datetime | None, None),
    }
    return model("mig_things", {**columns, **changes})


def test_first_plan_creates_tables_in_dependency_order(tmp_path: Path) -> None:
    owner = owners()
    result = plan([things(owner), owner], tmp_path, "initial")
    assert [o.description for o in result.operations] == [
        "create table mig_owners",
        "create table mig_things",
    ]
    assert result.path == tmp_path / "0001-initial.sql"
    text = result.path.read_text()
    assert '"name" varchar(40) NOT NULL' in text
    assert (
        '"owner_id" bigint NOT NULL REFERENCES "mig_owners" ("id") ON DELETE CASCADE'
        in text
    )
    assert '"tags" jsonb NOT NULL' in text
    assert '"seen" timestamptz,' in text or '"seen" timestamptz\n' in text
    state = json.loads((tmp_path / "0001-initial.state.json").read_text())
    assert state["tables"]["mig_things"]["columns"]["seen"]["nullable"] is True


def test_migration_state_is_backend_neutral_but_sql_is_backend_specific(
    tmp_path: Path,
) -> None:
    owner = owners()
    models = [things(owner), owner]
    postgres = plan(models, tmp_path / "postgres", write=False, dialect="postgres")
    sqlite = plan(models, tmp_path / "sqlite", write=False, dialect="sqlite")
    mysql = plan(models, tmp_path / "mysql", write=False, dialect="mysql")

    assert postgres.state == sqlite.state == mysql.state
    postgres_sql = "\n".join(operation.sql for operation in postgres.operations)
    sqlite_sql = "\n".join(operation.sql for operation in sqlite.operations)
    mysql_sql = "\n".join(operation.sql for operation in mysql.operations)
    assert '"id" uuid PRIMARY KEY' in postgres_sql
    assert '"id" UUID PRIMARY KEY' in sqlite_sql
    assert "`id` binary(16) PRIMARY KEY" in mysql_sql


def test_legacy_backend_specific_state_types_are_normalized(tmp_path: Path) -> None:
    owner = owners()
    models = [things(owner), owner]
    initial = plan(models, tmp_path, dialect="sqlite")
    assert initial.path is not None
    state_path = initial.path.with_suffix(".state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["tables"]["mig_owners"]["columns"]["id"]["type"] = "integer"
    state["tables"]["mig_things"]["columns"]["id"]["type"] = "UUID"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    upgraded = plan(models, tmp_path, dialect="mysql", write=False)

    assert upgraded.operations == ()


def test_migration_name_is_derived_and_can_be_overridden(tmp_path: Path) -> None:
    automatic = plan([owners()], tmp_path / "automatic")
    assert automatic.path == tmp_path / "automatic/0001-create-table-mig-owners.sql"

    explicit = plan([owners()], tmp_path / "explicit", "initial")
    assert explicit.path == tmp_path / "explicit/0001-initial.sql"


def test_nullable_set_null_foreign_key_is_written_to_migration_state(
    tmp_path: Path,
) -> None:
    owner = owners()
    nullable_ref = model(
        "mig_nullable_ref",
        {
            "id": (int, field(primary_key=True)),
            "owner_id": (
                int | None,
                foreign_key(owner, on_delete=OnDelete.SET_NULL, default=None),
            ),
        },
    )

    result = plan([nullable_ref, owner], tmp_path, "set-null")

    assert result.path is not None
    state = json.loads(result.path.with_suffix(".state.json").read_text())
    assert (
        state["tables"]["mig_nullable_ref"]["columns"]["owner_id"]["references"][
            "on_delete"
        ]
        == "set_null"
    )
    assert "ON DELETE SET NULL" in result.path.read_text()


def test_late_resolved_set_null_annotation_is_checked_during_planning(
    tmp_path: Path,
) -> None:
    owner = owners()
    alias = "_MigrationLateOwnerId"
    globals().pop(alias, None)
    nullable_ref = type(
        "LateNullableRef",
        (Model,),
        {
            "__module__": __name__,
            "__annotations__": {"id": int, "owner_id": alias},
            "id": field(primary_key=True),
            "owner_id": foreign_key(owner, on_delete=OnDelete.SET_NULL),
        },
        table="mig_late_nullable_ref",
    )
    globals()[alias] = int
    try:
        with pytest.raises(
            MigrationError, match=r"LateNullableRef\.owner_id.*set_null"
        ):
            plan([nullable_ref, owner], tmp_path, "late-set-null")
    finally:
        globals().pop(alias, None)


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize("option", [field(unique=True), field(index=True)])
def test_mysql_family_refuses_a_key_on_text_without_max_length(
    tmp_path: Path, dialect: str, option: Any
) -> None:
    keyed = model(
        "mig_keyed", {"id": (int, field(primary_key=True)), "token": (str, option)}
    )
    with pytest.raises(MigrationError, match=r"mig_keyed\.token.*max_length"):
        plan([keyed], tmp_path, "initial", dialect=dialect, write=False)


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_accepts_a_key_on_a_bounded_str(
    tmp_path: Path, dialect: str
) -> None:
    keyed = model(
        "mig_keyed",
        {
            "id": (int, field(primary_key=True)),
            "token": (str, field(unique=True, max_length=40)),
            "note": (str, None),
        },
    )
    result = plan([keyed], tmp_path, "initial", dialect=dialect, write=False)
    sql = "\n".join(operation.sql for operation in result.operations)
    assert "`token` varchar(40) NOT NULL UNIQUE" in sql
    assert "`note` text NOT NULL" in sql


def test_other_backends_keep_a_unique_text_column(tmp_path: Path) -> None:
    keyed = model(
        "mig_keyed",
        {"id": (int, field(primary_key=True)), "token": (str, field(unique=True))},
    )
    for dialect in ("postgres", "sqlite"):
        assert plan([keyed], tmp_path, "initial", dialect=dialect, write=False)


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_add_column_refuses_a_unique_text_column(
    tmp_path: Path, dialect: str
) -> None:
    base = model("mig_keyed", {"id": (int, field(primary_key=True))})
    plan([base], tmp_path, "initial", dialect=dialect)
    wider = model(
        "mig_keyed",
        {"id": (int, field(primary_key=True)), "token": (str, field(unique=True))},
    )
    with pytest.raises(MigrationError, match=r"mig_keyed\.token.*max_length"):
        plan([wider], tmp_path, "token", dialect=dialect, write=False)


def test_mysql_plan_uses_mysql_types_and_quoting(tmp_path: Path) -> None:
    owner = owners()
    result = plan([things(owner), owner], tmp_path, "mysql", dialect="mysql")
    assert result.path is not None
    text = result.path.read_text()
    assert "CREATE TABLE `mig_owners`" in text
    assert "`id` bigint PRIMARY KEY" in text
    assert "`name` varchar(40) NOT NULL" in text
    assert "ENGINE=InnoDB" in text
    assert "`id` binary(16) PRIMARY KEY" in text
    assert "`owner_id` bigint NOT NULL," in text
    assert (
        f"CONSTRAINT `{foreign_key_name('mig_things', 'owner_id', 'mysql')}` "
        "FOREIGN KEY (`owner_id`) "
        "REFERENCES `mig_owners` (`id`) ON DELETE CASCADE" in text
    )
    assert "`owner_id` bigint NOT NULL REFERENCES" not in text
    assert "`tags` json NOT NULL" in text
    assert "`seen` datetime(6)" in text


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_add_column_creates_foreign_key_separately(
    tmp_path: Path, dialect: str
) -> None:
    owner = owners()
    initial_child = model("mig_added_fk", {"id": (int, field(primary_key=True))})
    plan([owner, initial_child], tmp_path, "initial", dialect=dialect)
    child_with_reference = model(
        "mig_added_fk",
        {
            "id": (int, field(primary_key=True)),
            "owner_id": (
                int | None,
                foreign_key(owner, on_delete=OnDelete.CASCADE, default=None),
            ),
        },
    )

    result = plan(
        [owner, child_with_reference], tmp_path, "owner-reference", dialect=dialect
    )

    assert [operation.description for operation in result.operations] == [
        "add mig_added_fk.owner_id",
        "add foreign key mig_added_fk.owner_id",
    ]
    assert "ADD COLUMN `owner_id` bigint;" in result.operations[0].sql
    assert "REFERENCES" not in result.operations[0].sql
    assert (
        f"ADD CONSTRAINT `{foreign_key_name('mig_added_fk', 'owner_id', dialect)}` "
        "FOREIGN KEY" in result.operations[1].sql
    )


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_foreign_key_constraint_names_fit_identifier_limit(
    dialect: str,
) -> None:
    first = foreign_key_name("t" * 40, "c" * 30, dialect)
    second = foreign_key_name("t" * 40, "c" * 29 + "d", dialect)
    underscore_collision_a = foreign_key_name("a_b", "c", dialect)
    underscore_collision_b = foreign_key_name("a", "b_c", dialect)
    case_variant_a = foreign_key_name("Cases", "owner_id", dialect)
    case_variant_b = foreign_key_name("cases", "owner_id", dialect)

    assert len(first) == 64
    assert len(second) == 64
    assert first != second
    assert underscore_collision_a != underscore_collision_b
    assert case_variant_a.casefold() != case_variant_b.casefold()


def test_mysql_plan_uses_distinct_names_for_underscore_identifiers(
    tmp_path: Path,
) -> None:
    parent = model("fk_parent", {"id": (int, field(primary_key=True))})
    first = model(
        "a_b",
        {
            "id": (int, field(primary_key=True)),
            "c_id": (int, foreign_key(parent, on_delete=OnDelete.RESTRICT)),
        },
    )
    second = model(
        "a",
        {
            "id": (int, field(primary_key=True)),
            "b_c_id": (int, foreign_key(parent, on_delete=OnDelete.RESTRICT)),
        },
    )

    result = plan([parent, first, second], tmp_path, "underscores", dialect="mysql")
    sql = "\n".join(operation.sql for operation in result.operations)

    first_name = foreign_key_name("a_b", "c_id", "mysql")
    second_name = foreign_key_name("a", "b_c_id", "mysql")
    assert first_name != second_name
    assert f"CONSTRAINT `{first_name}`" in sql
    assert f"CONSTRAINT `{second_name}`" in sql


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_family_requires_manual_drop_of_foreign_key_column(
    tmp_path: Path, dialect: str
) -> None:
    owner = owners()
    with_reference = things(owner)
    plan([owner, with_reference], tmp_path, "initial", dialect=dialect)
    without_reference = model(
        "mig_things",
        {
            "id": (UUID, field(primary_key=True)),
            "tags": (dict[str, Any], None),
            "seen": (datetime | None, None),
        },
    )

    with pytest.raises(MigrationError, match="dropping a foreign-key column"):
        plan([owner, without_reference], tmp_path, "drop-reference", dialect=dialect)


def test_mysql_plan_rejects_unsupported_dialects(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="unsupported migration dialect"):
        plan([owners()], tmp_path, "oracle", dialect="oracle")


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        ("sqlite://./local.db", "sqlite"),
        ("postgresql://db/app", "postgres"),
        ("mysql://db/app", "mysql"),
        ("mariadb://db/app", "mariadb"),
    ],
)
def test_migration_plan_uses_database_url_backend(url: str, dialect: str) -> None:
    assert database_dialect(url) == dialect


def test_migration_plan_rejects_unknown_database_url_scheme() -> None:
    with pytest.raises(MigrationError, match="supported backend scheme"):
        database_dialect("oracle://db/app")


def test_migration_planner_rejects_type_mismatch_and_foreign_key_cycles() -> None:
    state = {
        "tables": {
            "child": {
                "columns": {
                    "owner_id": {
                        "type": "text",
                        "references": {"table": "owner", "column": "id"},
                    }
                }
            }
        }
    }
    external = {"tables": {"owner": {"columns": {"id": {"type": "bigint"}}}}}
    with pytest.raises(MigrationError, match="is text but"):
        check_references(state, external)
    cycle = {
        "a": {"columns": {"b_id": {"references": {"table": "b"}}}},
        "b": {"columns": {"a_id": {"references": {"table": "a"}}}},
    }
    with pytest.raises(MigrationError, match="form a cycle"):
        creation_order(cycle)


def test_mysql_foreign_key_sql_refuses_non_foreign_key_columns() -> None:
    with pytest.raises(MigrationError, match="is not a foreign key"):
        foreign_key_sql("items", "name", {"references": None}, "mysql")


def test_column_changes_cover_mysql_and_not_null_plans() -> None:
    previous = {
        "type": "text",
        "nullable": True,
        "primary_key": False,
        "unique": False,
        "references": None,
    }
    current = {**previous, "nullable": False}
    mysql = alter_column("items", "name", previous, current, "mysql")
    postgres = alter_column("items", "name", previous, current, "postgres")
    assert "MODIFY COLUMN" in mysql[0].sql
    assert "SET NOT NULL" in postgres[0].sql


def column_state(**changes: Any) -> dict[str, Any]:
    return {
        "type": "text",
        "nullable": False,
        "primary_key": False,
        "unique": False,
        "references": None,
        **changes,
    }


def test_new_indexes_follow_the_columns_they_use_and_old_ones_go_first() -> None:
    id_column = column_state(type="integer", primary_key=True)
    old = {"columns": {"id": id_column, "gone": column_state()}, "indexes": [["gone"]]}
    new = {
        "columns": {"id": id_column, "added": column_state(type="varchar(20)")},
        "indexes": [["added"]],
    }
    for dialect in ("postgres", "mysql", "mariadb"):
        steps = [o.description for o in table_changes("items", old, new, dialect)]
        assert steps == [
            "drop index items (gone)",
            "add items.added",
            "index items (added)",
            "drop items.gone",
        ]


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize("keys", [{"unique": True}, {"primary_key": True}])
def test_mysql_family_modify_does_not_repeat_key_clauses(
    dialect: str, keys: dict[str, bool]
) -> None:
    previous = column_state(nullable=True, **keys)
    current = column_state(nullable=False, **keys)
    (operation,) = alter_column("items", "name", previous, current, dialect)
    assert "UNIQUE" not in operation.sql
    assert "PRIMARY KEY" not in operation.sql
    assert "NOT NULL" in operation.sql


def test_sqlite_rebuild_warnings_cover_added_required_and_changed_columns() -> None:
    previous = {
        "columns": {
            "name": {
                "type": "text",
                "nullable": True,
                "primary_key": False,
                "unique": False,
                "references": None,
            }
        },
        "unique": [],
    }
    current = {
        "columns": {
            "name": {
                **cast(dict[str, Any], previous["columns"])["name"],
                "nullable": False,
            },
            "required": {
                "type": "text",
                "nullable": False,
                "primary_key": False,
                "unique": False,
                "references": None,
            },
        },
        "unique": [["name"]],
    }
    warnings = _sqlite_rebuild_warnings(previous, current)
    assert any("requires values" in item for item in warnings)
    assert any("unique constraints" in item for item in warnings)
    assert any("existing rows contain NULL" in item for item in warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_url", "dialect"),
    [
        pytest.param(
            MYSQL_URL,
            "mysql",
            marks=pytest.mark.skipif(
                MYSQL_URL is None, reason="HINOKI_TEST_MYSQL_URL is not set"
            ),
            id="mysql",
        ),
        pytest.param(
            MARIADB_URL,
            "mariadb",
            marks=pytest.mark.skipif(
                MARIADB_URL is None, reason="HINOKI_TEST_MARIADB_URL is not set"
            ),
            id="mariadb",
        ),
    ],
)
async def test_mysql_family_migrations_and_model_returning_contract(
    tmp_path: Path, database_url: str | None, dialect: str
) -> None:
    assert database_url is not None
    owner = owners()
    thing = things(owner)
    database = await Database.connect(database_url, max_size=3)
    assert database.dialect == dialect
    for table in ("mig_things", "mig_owners", "hinoki_migrations"):
        await database.execute(f"DROP TABLE IF EXISTS `{table}`")
    plan([thing, owner], tmp_path, "initial", dialect=dialect)
    assert await migrator.apply(database, tmp_path) == ["0001-initial"]
    assert await migrator.apply(database, tmp_path) == []
    with use_database(database):
        await owner.acreate(id=7, name="owner")
        created: Any = await thing.acreate(
            id=UUID(int=1), owner_id=7, tags={"ready": True}, seen=None
        )
        assert created.id == UUID(int=1)
        updated: Any = await thing.filter(id=UUID(int=1)).aupdate(
            {"tags": {"ready": False}}
        )
        assert len(updated) == 1
        assert updated[0].tags == {"ready": False}
        await owner.filter(id=7).adelete()
        assert (
            await database.fetch(
                "SELECT id FROM `mig_things` WHERE id = ?", UUID(int=1)
            )
            == []
        )
    invalid_database = await Database.connect(database_url, max_size=1)
    with use_database(invalid_database), pytest.raises(ForeignKeyViolation):
        await thing.acreate(id=UUID(int=2), owner_id=999, tags={}, seen=None)
    invalid_database.close()
    for table in ("mig_things", "mig_owners", "hinoki_migrations"):
        await database.execute(f"DROP TABLE IF EXISTS `{table}`")
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "database_url",
    [
        pytest.param(
            MYSQL_URL,
            marks=pytest.mark.skipif(
                MYSQL_URL is None, reason="HINOKI_TEST_MYSQL_URL is not set"
            ),
            id="mysql",
        ),
        pytest.param(
            MARIADB_URL,
            marks=pytest.mark.skipif(
                MARIADB_URL is None, reason="HINOKI_TEST_MARIADB_URL is not set"
            ),
            id="mariadb",
        ),
    ],
)
async def test_mysql_family_failed_migration_reports_implicit_ddl_commit(
    tmp_path: Path, database_url: str | None
) -> None:
    assert database_url is not None
    database = await Database.connect(database_url, max_size=2)
    table = "hinoki_mysql_family_partial_migration"
    await database.execute(f"DROP TABLE IF EXISTS `{table}`")
    path = tmp_path / "0001-partial.sql"
    path.write_text(
        f"CREATE TABLE `{table}` (`id` BIGINT PRIMARY KEY) ENGINE=InnoDB;\n"
        "CREATE TABLE broken syntax;\n",
        encoding="utf-8",
    )
    await migrator.status(database, tmp_path)
    with pytest.raises(MigrationError, match="may be partially applied"):
        await migrator.apply_one(database, path)
    rows = await database.fetch(
        "SELECT COUNT(*) AS `count` FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = ?",
        table,
    )
    assert rows[0]["count"] == 1
    await database.execute(f"DROP TABLE `{table}`")
    database.close()


def test_models_are_importable_from_a_console_script() -> None:
    project = str(Path(__file__).resolve().parents[1])
    examples = str(Path(project) / "examples")
    original_path = sys.path.copy()
    sys.path[:] = [entry for entry in sys.path if entry not in {"", project, examples}]
    try:
        manifest = load_manifest(Path(examples) / "hinoki.toml")
        found = manifest.models(manifest.app("notes"))
    finally:
        sys.path[:] = original_path
    assert [item.__name__ for item in found] == ["Note"]


def test_an_unchanged_model_plans_nothing(tmp_path: Path) -> None:
    owner = owners()
    plan([owner], tmp_path, "initial")
    again = plan([owner], tmp_path, "again")
    assert again.operations == ()
    assert again.path is None
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "0001-initial.sql",
        "0001-initial.state.json",
    ]


def test_safe_changes_are_numbered_and_written(tmp_path: Path) -> None:
    plan([owners()], tmp_path, "initial")
    bigger = owners({"note": (str | None, None), "bio": (str | None, None)})
    result = plan([bigger], tmp_path, "notes")
    assert result.path == tmp_path / "0002-notes.sql"
    assert result.path.read_text().count("ADD COLUMN") == 2
    relaxed = owners({"note": (str | None, None), "bio": (str | None, None)})
    assert plan([relaxed], tmp_path, "same").operations == ()


def test_destructive_operations_are_refused_unless_allowed(tmp_path: Path) -> None:
    plan([owners({"nick": (str | None, None)})], tmp_path, "initial")
    with pytest.raises(DestructiveMigration, match=r"drop mig_owners\.nick"):
        plan([owners()], tmp_path, "drop")
    assert len(list(tmp_path.glob("*.sql"))) == 1  # nothing was written
    result = plan([owners()], tmp_path, "drop", allow_destructive=True)
    assert result.path is not None
    assert "-- DESTRUCTIVE: drops the column and its data" in result.path.read_text()


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"age": (int, None)}, "NOT NULL column without a default"),
    ],
)
def test_required_new_columns_are_flagged(
    tmp_path: Path, extra: dict[str, tuple[Any, Any]], reason: str
) -> None:
    plan([owners()], tmp_path, "initial")
    with pytest.raises(DestructiveMigration, match=reason):
        plan([owners(extra)], tmp_path, "age")


def test_type_and_nullability_changes(tmp_path: Path) -> None:
    plan([owners({"score": (int, None)})], tmp_path, "initial")
    changed = owners({"score": (float | None, None)})
    with pytest.raises(DestructiveMigration, match="column type"):
        plan([changed], tmp_path, "score")
    result = plan([changed], tmp_path, "score", allow_destructive=True, write=False)
    assert [o.description for o in result.operations] == [
        "change mig_owners.score to double precision",
        "allow NULL in mig_owners.score",
    ]
    assert result.path is None


def test_dropping_a_table_comes_after_its_dependents(tmp_path: Path) -> None:
    owner = owners()
    plan([owner, things(owner)], tmp_path, "initial")
    result = plan([], tmp_path, "gone", allow_destructive=True, write=False)
    assert [o.description for o in result.operations] == [
        "drop table mig_things",
        "drop table mig_owners",
    ]


def test_unplannable_changes_ask_for_hand_written_sql(tmp_path: Path) -> None:
    plan([owners({"code": (str, None)})], tmp_path, "initial")
    unique = owners({"code": (str, field(unique=True))})
    with pytest.raises(MigrationError, match="write that migration by hand"):
        plan([unique], tmp_path, "unique")


def test_model_declaration_problems_fail_the_plan(tmp_path: Path) -> None:
    owner = owners()
    cases: list[list[type[Model]]] = [
        [model("a", {"id": (int, field(primary_key=True)), "x": (object, None)})],
        [model("a", {"id": (int, field(primary_key=True)), "x": (int | str, None)})],
        [
            things(
                owner, owner_id=(UUID, foreign_key(owner, on_delete=OnDelete.CASCADE))
            )
        ],
    ]
    for models in cases:
        with pytest.raises(MigrationError):
            plan(models, tmp_path, "bad", write=False)


def test_names_and_identifiers_are_validated(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="names use"):
        plan([owners()], tmp_path, "Bad_Name")
    with pytest.raises(MigrationError, match="invalid identifier"):
        plan(
            [model('x"; DROP TABLE y; --', {"id": (int, field(primary_key=True))})],
            tmp_path,
            "x",
        )


def test_a_missing_state_file_is_reported(tmp_path: Path) -> None:
    plan([owners()], tmp_path, "initial")
    (tmp_path / "0001-initial.state.json").unlink()
    with pytest.raises(MigrationError, match=r"state\.json is missing"):
        plan([owners()], tmp_path, "next")


def test_statements_split_on_line_terminators() -> None:
    text = (
        "-- note\n\nCREATE TABLE a (\n    x int,\n    y int\n);\n-- c\nDROP TABLE b;\n"
    )
    assert statements(text) == [
        "CREATE TABLE a (\n    x int,\n    y int\n)",
        "DROP TABLE b",
    ]
    with pytest.raises(MigrationError, match="does not end"):
        statements("SELECT 1")


def test_models_of_finds_the_models_of_a_module() -> None:
    names = [m.__name__ for m in models_of("blog.models")]
    assert names == ["User", "Post"]


def test_command_line_plans_and_prints(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["migrations", "--dir", str(tmp_path)]
    plan_args = [*args, "plan", "--models", "blog.models", "--name", "initial"]
    assert main([*plan_args, "--dry-run"]) == 0
    assert list(tmp_path.iterdir()) == []
    assert main(plan_args) == 0
    assert main([*args, "sql", "0001-initial"]) == 0
    out = capsys.readouterr().out
    assert "create table blog_posts" in out
    assert "CREATE TABLE" in out
    assert main([*args, "sql", "9999-nope"]) == 1
    assert main([*args, "apply"]) == 0  # local SQLite default
    assert (tmp_path / ".hinoki" / "local.sqlite3").is_file()
    assert main([*args, "status"]) == 0
    assert "applied" in capsys.readouterr().out


def test_plan_check_is_read_only_and_keeps_dry_run_preview_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("migration plan check connected to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)
    directory = tmp_path / "migrations"
    args = [
        "migrations",
        "--dir",
        str(directory),
        "plan",
        "--models",
        "blog.models",
    ]

    assert main([*args, "--check"]) == 1
    check_output = capsys.readouterr().out
    assert "create table blog_posts" in check_output
    assert "pending migrations:" in check_output
    assert not directory.exists()

    assert main([*args, "--dry-run"]) == 0
    assert "create table blog_posts" in capsys.readouterr().out
    assert not directory.exists()

    assert main(args) == 0
    capsys.readouterr()
    paths = sorted(directory.iterdir())
    contents = [path.read_bytes() for path in paths]
    assert main([*args, "--check"]) == 0
    assert capsys.readouterr().out == "no changes\n"
    assert sorted(directory.iterdir()) == paths
    assert [path.read_bytes() for path in paths] == contents


def test_plan_check_reports_destructive_changes_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    async def fail_connect(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("migration plan check connected to a database")

    monkeypatch.setattr(Database, "connect", fail_connect)
    monkeypatch.setattr("hinoki.__main__.models_of", lambda _module: [owners()])
    directory = tmp_path / "migrations"
    args = [
        "migrations",
        "--dir",
        str(directory),
        "plan",
        "--models",
        "example.models",
    ]
    assert main(args) == 0
    capsys.readouterr()
    paths = sorted(directory.iterdir())
    contents = [path.read_bytes() for path in paths]

    monkeypatch.setattr("hinoki.__main__.models_of", lambda _module: [])
    assert main([*args, "--check"]) == 1
    output = capsys.readouterr().out
    assert "DESTRUCTIVE drop table mig_owners" in output
    assert "pending migrations:" in output
    assert sorted(directory.iterdir()) == paths
    assert [path.read_bytes() for path in paths] == contents

    assert main([*args, "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert "DESTRUCTIVE drop table mig_owners" in preview
    assert sorted(directory.iterdir()) == paths
    assert [path.read_bytes() for path in paths] == contents


@pytest.mark.parametrize(
    "option", [["--dry-run"], ["--name", "renamed"], ["--allow-destructive"]]
)
def test_plan_check_rejects_write_and_preview_options(
    option: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["migrations", "plan", "--models", "does.not.load", "--check", *option]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "cannot be combined with" in captured.err
    assert captured.out == ""


def test_command_line_reports_a_failed_connection_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    # Port 1 refuses connections; the URL's password must not be echoed.
    url = "postgres://someone:hunter2@127.0.0.1:1/nowhere?connect_timeout=2"
    args = ["migrations", "--dir", str(tmp_path)]
    for action in ("status", "apply"):
        assert main([*args, action, "--database", url]) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("error: database: ")
        assert "fix:" in captured.err and "HINOKI_DATABASE_URL" in captured.err
        assert "Traceback" not in captured.err and "hunter2" not in captured.err
        assert captured.out == ""


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    assert URL is not None
    database = await Database.connect(URL, max_size=2)
    for table in ("mig_things", "mig_owners", "hinoki_migrations"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    yield database
    for table in ("mig_things", "mig_owners", "hinoki_migrations"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    database.close()


@needs_database
async def test_apply_creates_tables_once_and_status_tracks_it(
    db: Database, tmp_path: Path
) -> None:
    owner = owners()
    plan([owner, things(owner)], tmp_path, "initial")
    assert await migrator.status(db, tmp_path) == [("0001-initial", "pending")]
    assert await migrator.apply(db, tmp_path) == ["0001-initial"]
    assert await migrator.apply(db, tmp_path) == []
    assert await migrator.status(db, tmp_path) == [("0001-initial", "applied")]
    await db.execute("INSERT INTO mig_owners (id, name) VALUES (1, 'x')")
    plan([owners({"note": (str | None, None)}), things(owner)], tmp_path, "note")
    assert await migrator.apply(db, tmp_path) == ["0002-note"]
    await db.execute("UPDATE mig_owners SET note = 'n'")


@needs_database
async def test_edited_or_missing_applied_files_are_errors(
    db: Database, tmp_path: Path
) -> None:
    plan([owners()], tmp_path, "initial")
    await migrator.apply(db, tmp_path)
    path = tmp_path / "0001-initial.sql"
    original = path.read_text()
    path.write_text(original + "-- edited\n")
    assert await migrator.status(db, tmp_path) == [("0001-initial", "changed")]
    with pytest.raises(MigrationError, match="changed"):
        await migrator.apply(db, tmp_path)
    path.unlink()
    assert await migrator.status(db, tmp_path) == [("0001-initial", "missing")]
    with pytest.raises(MigrationError, match="missing"):
        await migrator.apply(db, tmp_path)


@needs_database
async def test_a_failing_migration_rolls_back_and_is_not_recorded(
    db: Database, tmp_path: Path
) -> None:
    (tmp_path / "0001-broken.sql").write_text(
        "CREATE TABLE mig_owners (id int);\nSELECT * FROM nope;\n"
    )
    with pytest.raises(MigrationError, match="0001-broken failed"):
        await migrator.apply(db, tmp_path)
    assert await db.fetch("SELECT to_regclass('mig_owners')::text AS t") == [
        {"t": None}
    ]
    assert await migrator.status(db, tmp_path) == [("0001-broken", "pending")]


def test_migrations_take_the_app_as_an_argument_or_as_an_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "argumentapp"
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "migrations" / "__init__.py").write_text("")
    (package / "models.py").write_text(
        "from hinoki.orm import Model, field\n\n\n"
        "class Note(Model, table='argument_notes'):\n"
        "    id: int = field(primary_key=True, default=0)\n"
        "    title: str = ''\n"
    )
    (tmp_path / "hinoki.toml").write_text(
        '[project]\napplication = "argumentapp.main:app"\n\n'
        '[[apps]]\nname = "argumentapp"\nmodels = "argumentapp.models"\n'
        'migrations = "argumentapp.migrations"\nrequires = []\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    written = package / "migrations"

    assert main(["migrations", "plan", "argumentapp", "--check"]) == 1
    assert main(["migrations", "plan", "--app", "argumentapp", "--check"]) == 1
    assert main(["migrations", "plan", "argumentapp", "--name", "initial"]) == 0
    assert [path.name for path in written.glob("*.sql")] == ["0001-initial.sql"]
    assert main(["migrations", "plan", "argumentapp", "--check"]) == 0
    capsys.readouterr()

    assert main(["migrations", "apply", "argumentapp"]) == 0
    assert "0001-initial" in capsys.readouterr().out
    assert main(["migrations", "apply", "--app", "argumentapp"]) == 0
    assert main(["migrations", "status", "argumentapp"]) == 0
    assert "applied" in capsys.readouterr().out
    assert main(["migrations", "plan", "missing"]) == 1
    assert main(["migrations", "apply", "argumentapp", "--app", "argumentapp"]) == 0
    assert main(["migrations", "apply", "argumentapp", "--app", "other"]) == 2
    assert "conflicts with --app" in capsys.readouterr().err


def test_text_to_numeric_is_planned_only_where_the_backend_stores_it_differently() -> (
    None
):
    column = {
        "type": "text",
        "nullable": False,
        "primary_key": False,
        "unique": False,
        "references": None,
    }
    old = {"columns": {"price": column}, "unique": []}
    new = {"columns": {"price": {**column, "type": "numeric"}}, "unique": []}
    assert table_changes("items", old, new, "sqlite") == []
    (postgres,) = table_changes("items", old, new, "postgres")
    assert 'ALTER COLUMN "price" TYPE numeric USING "price"::numeric' in (postgres.sql)
    (mysql,) = table_changes("items", old, new, "mysql")
    assert "MODIFY COLUMN `price` decimal(65,30) NOT NULL" in mysql.sql
