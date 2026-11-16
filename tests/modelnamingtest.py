"""Stable Model table/display defaults and aggregate ownership guards."""

from __future__ import annotations

import dataclasses
import types
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki.admin import Admin
from hinoki.db import Database
from hinoki.errors import DestructiveMigration, MigrationError, RegistrationError
from hinoki.orm import Model, field, migrator, use_database
from hinoki.orm.migrations import plan, state_of
from hinoki.project import ProjectApp, ProjectError, ProjectManifest


def declare_model(name: str, **options: object) -> type[Model]:
    def body(namespace: dict[str, object]) -> None:
        namespace["__module__"] = __name__
        namespace["__annotations__"] = {"id": int}
        namespace["id"] = field(primary_key=True)

    return types.new_class(name, (Model,), kwds=options, exec_body=body)


def test_model_names_use_frozen_snake_case_and_suffix_rules() -> None:
    class Invoice(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class UserProfile(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class APIKey(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Category(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Address(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Status(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Person(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Analysis(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Users(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Version2(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)

    class Box(Model):
        id: int = field(primary_key=True)

    class Brush(Model):
        id: int = field(primary_key=True)

    class Quiz(Model):
        id: int = field(primary_key=True)

    class Match(Model):
        id: int = field(primary_key=True)

    assert (Invoice.__table__, Invoice.__label__, Invoice.__plural__) == (
        "invoices",
        "Invoice",
        "Invoices",
    )
    assert (UserProfile.__table__, UserProfile.__label__, UserProfile.__plural__) == (
        "user_profiles",
        "User profile",
        "User profiles",
    )
    assert (APIKey.__table__, APIKey.__label__, APIKey.__plural__) == (
        "api_keys",
        "API key",
        "API keys",
    )
    assert Category.__table__ == "categories"
    assert Address.__table__ == "addresses"
    assert Status.__table__ == "statuses"
    assert Person.__table__ == "persons" and Person.__plural__ == "Persons"
    assert Analysis.__table__ == "analysises" and Analysis.__plural__ == "Analysises"
    assert Users.__table__ == "userses" and Users.__plural__ == "Userses"
    assert Version2.__table__ == "version2s"
    assert Box.__table__ == "boxes"
    assert Brush.__table__ == "brushes"
    assert Quiz.__table__ == "quizes"
    assert Match.__table__ == "matches"


def test_explicit_table_and_display_overrides_are_independent() -> None:
    class Record(Model, table="account_records", label="Account holder"):
        id: int = field(primary_key=True)

    class Localized(Model, table="utenti", label="Utente", plural="Utenti"):
        id: int = field(primary_key=True)

    class Acronym(Model, table="api_keys", plural="API credentials"):
        id: int = field(primary_key=True)

    assert (Record.__table__, Record.__label__, Record.__plural__) == (
        "account_records",
        "Account holder",
        "Account holders",
    )
    assert (Localized.__label__, Localized.__plural__) == ("Utente", "Utenti")
    assert (Acronym.__label__, Acronym.__plural__) == ("Acronym", "API credentials")
    assert Record.__table_is_implicit__ is False


def test_non_word_class_name_accepts_complete_explicit_names() -> None:
    class _(Model, table="records", label="Record", plural="Records"):
        id: int = field(primary_key=True)

    assert (_.__table__, _.__label__, _.__plural__) == (
        "records",
        "Record",
        "Records",
    )


@pytest.mark.parametrize("model_name", ["SqliteCache", "HinokiMigration"])
def test_unsafe_implicit_table_names_fail_at_declaration(model_name: str) -> None:
    with pytest.raises(RegistrationError, match="derived table"):
        declare_model(model_name)


def test_non_ascii_implicit_table_needs_an_override_and_label_is_retained() -> None:
    with pytest.raises(RegistrationError, match="portable SQL identifier"):
        declare_model("Città")

    Utente = declare_model("Città", table="utenti")

    assert Utente.__label__ == "Città"


def test_name_length_and_control_characters_are_rejected() -> None:
    with pytest.raises(RegistrationError, match="portable SQL identifier"):
        declare_model("A" * 64)
    with pytest.raises(RegistrationError, match="control characters"):
        declare_model("CleanName", table="clean_names", label="bad\nlabel")


def test_abstract_model_rejects_a_table_override() -> None:
    with pytest.raises(RegistrationError, match="abstract model has no table"):

        class AbstractWithTable(Model, abstract=True, table="not_allowed"):
            id: int = field(primary_key=True)


def test_name_override_outputs_do_not_change_migration_state(tmp_path: Path) -> None:
    class First(Model, table="stable_records", label="Record", plural="Records"):
        id: int = field(primary_key=True)

    class RenamedDisplay(
        Model, table="stable_records", label="Entry", plural="Entries"
    ):
        id: int = field(primary_key=True)

    plan([First], tmp_path, "initial")
    assert plan([RenamedDisplay], tmp_path, write=False).operations == ()


def test_renaming_an_implicit_model_keeps_destructive_migration_guard(
    tmp_path: Path,
) -> None:
    class OldInvoice(Model):
        id: int = field(primary_key=True)

    class NewInvoice(Model):
        id: int = field(primary_key=True)

    plan([OldInvoice], tmp_path, "initial")
    with pytest.raises(DestructiveMigration, match="destructive operations"):
        plan([NewInvoice], tmp_path, "rename")


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("table", 3), ("label", None), ("plural", False)],
)
def test_name_options_reject_non_strings(keyword: str, value: object) -> None:
    with pytest.raises(RegistrationError, match=f"{keyword}= must be a string"):
        declare_model("BadName", **{keyword: value})


def test_empty_table_sentinel_and_abstract_inheritance_reset_provenance() -> None:
    class ExplicitParent(Model, table="legacy_rows"):
        id: int = field(primary_key=True)

    class InferredChild(ExplicitParent):
        pass

    class ExplicitChild(ExplicitParent, table="chosen_rows"):
        pass

    class AbstractBase(Model, abstract=True):
        id: int = field(primary_key=True)

    class AbstractChild(AbstractBase, abstract=True):
        pass

    class ConcreteChild(AbstractChild, table=""):
        pass

    assert ExplicitParent.__table_is_implicit__ is False
    assert InferredChild.__table__ == "inferred_childs"
    assert InferredChild.__table_is_implicit__ is True
    assert ExplicitChild.__table_is_implicit__ is False
    assert AbstractBase.__table__ == AbstractChild.__table__ == ""
    assert AbstractBase.__table_is_implicit__ is False
    assert AbstractChild.__table_is_implicit__ is False
    assert ConcreteChild.__table__ == "concrete_childs"
    assert ConcreteChild.__table_is_implicit__ is True
    assert "__table_is_implicit__" not in {
        item.name for item in dataclasses.fields(InferredChild)
    }
    assert "__table_is_implicit__" not in state_of([InferredChild])


def test_table_state_rejects_duplicate_and_ambiguous_owners() -> None:
    class Invoice(Model):
        id: int = field(primary_key=True)

    class ExplicitInvoice(Model, table="invoices"):
        id: int = field(primary_key=True)

    class MixedCase(Model, table="Invoices"):
        id: int = field(primary_key=True)

    with pytest.raises(MigrationError, match="both claim"):
        state_of([Invoice, ExplicitInvoice])
    with pytest.raises(MigrationError, match="both claim"):
        state_of([Invoice, MixedCase])
    assert set(state_of([Invoice, Invoice])["tables"]) == {"invoices"}
    assert set(state_of([Invoice], reference_models=[Invoice])["tables"]) == {
        "invoices"
    }


def test_explicit_case_distinct_tables_keep_legacy_boundary() -> None:
    class UpperTable(Model, table="RecordRows"):
        id: int = field(primary_key=True)

    class LowerTable(Model, table="recordrows"):
        id: int = field(primary_key=True)

    assert set(state_of([UpperTable, LowerTable])["tables"]) == {
        "RecordRows",
        "recordrows",
    }


def test_migration_ledger_cannot_be_owned_by_model_state() -> None:
    class LedgerModel(Model, table="Hinoki_Migrations"):
        id: int = field(primary_key=True)

    with pytest.raises(MigrationError, match="migration ledger"):
        state_of([LedgerModel])
    with pytest.raises(MigrationError, match="migration ledger"):
        state_of([], reference_models=[LedgerModel])


def test_migration_plan_rejects_ledger_collision_before_writing(tmp_path: Path) -> None:
    LedgerModel = declare_model("AuditRows", table="HINOKI_MIGRATIONS")
    with pytest.raises(MigrationError, match="migration ledger"):
        plan([LedgerModel], tmp_path, "initial")
    assert not list(tmp_path.iterdir())


def test_project_rejects_shared_model_ownership_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Invoice(Model):
        id: int = field(primary_key=True)

    apps = (
        ProjectApp("first", "tests.shared", "first.migrations", ()),
        ProjectApp("second", "tests.shared", "second.migrations", ()),
    )
    manifest = ProjectManifest(tmp_path / "hinoki.toml", "app:app", apps)
    monkeypatch.setattr(ProjectManifest, "models", lambda _self, _app: [Invoice])
    with pytest.raises(ProjectError, match=r"in app 'first'.*in app 'second'"):
        manifest.all_models()

    class Ledger(Model, table="hinoki_migrations"):
        id: int = field(primary_key=True)

    monkeypatch.setattr(ProjectManifest, "models", lambda _self, _app: [Ledger])
    with pytest.raises(ProjectError, match="migration ledger"):
        manifest.all_models()


def test_admin_table_ownership_is_separate_from_slug_and_same_model_aliases_work() -> (
    None
):
    class Invoice(Model):
        id: int = field(primary_key=True)

    class ExplicitInvoice(Model, table="Invoices"):
        id: int = field(primary_key=True)

    admin = Admin(access=None, secret="s" * 32)  # type: ignore[arg-type]
    admin.register(Invoice, slug="invoice")
    with pytest.raises(RegistrationError, match="conflicting table names"):
        admin.register(ExplicitInvoice, slug="legacy-invoice")

    aliases = Admin(access=None, secret="s" * 32)  # type: ignore[arg-type]
    aliases.register(Invoice, slug="invoice")
    aliases.register(Invoice, slug="invoice-alias")
    assert set(aliases.models) == {"invoice", "invoice-alias"}


@pytest.mark.asyncio
async def test_derived_table_migrates_and_round_trips_on_sqlite(tmp_path: Path) -> None:
    class LocalNote(Model):
        id: UUID = field(primary_key=True, default_factory=uuid4)
        title: str

    migration_dir = tmp_path / "migrations"
    plan([LocalNote], migration_dir, "initial")
    database = await Database.connect(f"sqlite:///{tmp_path / 'local.sqlite3'}")
    try:
        await migrator.apply(database, migration_dir)
        with use_database(database):
            created = await LocalNote.acreate(title="derived")
            found = await LocalNote.aget(id=created.id)
        assert found.title == "derived"
    finally:
        database.close()
