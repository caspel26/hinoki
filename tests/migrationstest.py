"""Migration planning (no database) and applying (disposable PostgreSQL)."""

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hinoki.__main__ import main
from hinoki.db import Database
from hinoki.errors import DestructiveMigration, MigrationError
from hinoki.orm import Model, field, foreign_key, migrator
from hinoki.orm.migrations import models_of, plan, statements

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")
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
        "owner_id": (int, foreign_key(owner, on_delete="cascade")),
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
        [things(owner, owner_id=(UUID, foreign_key(owner, on_delete="cascade")))],
        [
            model(
                "a",
                {
                    "id": (int, field(primary_key=True)),
                    "o_id": (int, foreign_key(owner, on_delete="set_null")),
                },
            ),
            owner,
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
    names = [m.__name__ for m in models_of("examples.blog.models")]
    assert names == ["User", "Post"]


def test_command_line_plans_and_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["migrations", "--dir", str(tmp_path)]
    plan_args = [*args, "plan", "--models", "examples.blog.models", "--name", "initial"]
    assert main([*plan_args, "--dry-run"]) == 0
    assert list(tmp_path.iterdir()) == []
    assert main(plan_args) == 0
    assert main([*args, "sql", "0001-initial"]) == 0
    out = capsys.readouterr().out
    assert "create table blog_posts" in out
    assert "CREATE TABLE" in out
    assert main([*args, "sql", "9999-nope"]) == 1
    assert main([*args, "apply"]) == 1  # no database configured
    error = capsys.readouterr().err
    assert "error:" in error and "fix:" in error


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
