"""Stable Model table/display defaults and aggregate ownership guards."""

from __future__ import annotations

import dataclasses
import types
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hinoki import db
from hinoki.admin import Admin
from hinoki.db import Database
from hinoki.errors import DestructiveMigration, MigrationError, RegistrationError
from hinoki.orm import (
    Model,
    OnDelete,
    field,
    foreign_key,
    migrator,
    pk,
    use_database,
)
from hinoki.orm.migrations import plan, state_of
from hinoki.orm.naming import app_label
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
        "modelnamingtest_invoices",
        "Invoice",
        "Invoices",
    )
    assert (UserProfile.__table__, UserProfile.__label__, UserProfile.__plural__) == (
        "modelnamingtest_user_profiles",
        "User profile",
        "User profiles",
    )
    assert (APIKey.__table__, APIKey.__label__, APIKey.__plural__) == (
        "modelnamingtest_api_keys",
        "API key",
        "API keys",
    )
    assert Category.__table__ == "modelnamingtest_categories"
    assert Category.__plural__ == "Categories"
    assert Address.__table__ == "modelnamingtest_addresses"
    assert Status.__table__ == "modelnamingtest_statuses"
    assert Person.__table__ == "modelnamingtest_persons"
    assert Person.__plural__ == "Persons"
    assert Analysis.__table__ == "modelnamingtest_analysises"
    assert Analysis.__plural__ == "Analysises"
    assert (
        Users.__table__ == "modelnamingtest_userses" and Users.__plural__ == "Userses"
    )
    assert Version2.__table__ == "modelnamingtest_version2s"
    assert Box.__table__ == "modelnamingtest_boxes" and Box.__plural__ == "Boxes"
    assert Brush.__table__ == "modelnamingtest_brushes"
    assert Quiz.__table__ == "modelnamingtest_quizes"
    assert Match.__table__ == "modelnamingtest_matches"


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


@pytest.mark.parametrize(
    ("app", "model_name"), [("sqlite", "Cache"), ("hinoki", "Migration")]
)
def test_unsafe_implicit_table_names_fail_when_the_model_is_used(
    app: str, model_name: str
) -> None:
    Unsafe = declare_model(model_name, app=app)

    with pytest.raises(RegistrationError, match=r"derived table.*@db\.table"):
        Unsafe.all()
    with pytest.raises(MigrationError, match="derived table"):
        state_of([Unsafe])
    with pytest.raises(MigrationError, match="derived table"):
        state_of([], reference_models=[Unsafe])


def test_table_decorator_rescues_a_class_whose_derived_name_is_rejected() -> None:
    @db.table("ledger")
    class HinokiMigration(Model):
        id: int = pk()

    Cafe = db.table("cafes")(declare_model("Café"))

    assert (HinokiMigration.__table__, Cafe.__table__) == ("ledger", "cafes")
    assert HinokiMigration.all().compile("select")[0] == 'SELECT "id" FROM "ledger"'
    assert set(state_of([HinokiMigration, Cafe])["tables"]) == {"ledger", "cafes"}


def test_non_ascii_implicit_table_needs_an_override_and_label_is_retained() -> None:
    with pytest.raises(RegistrationError, match="portable SQL identifier"):
        declare_model("Città").all()

    Utente = declare_model("Città", table="utenti")

    assert Utente.__label__ == "Città"


def test_name_length_and_control_characters_are_rejected() -> None:
    with pytest.raises(RegistrationError, match="portable SQL identifier"):
        declare_model("A" * 64).all()
    with pytest.raises(RegistrationError, match="control characters"):
        declare_model("CleanName", table="clean_names", label="bad\nlabel")


def test_unusable_derived_table_is_refused_as_a_foreign_key_target() -> None:
    Unsafe = declare_model("Cache", app="sqlite")

    with pytest.raises(RegistrationError, match="derived table 'sqlite_caches'"):

        class CacheEntry(Model):
            id: int = pk()
            cache_id: int = foreign_key(Unsafe, on_delete=OnDelete.CASCADE)


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
    assert InferredChild.__table__ == "modelnamingtest_inferred_childs"
    assert InferredChild.__table_is_implicit__ is True
    assert ExplicitChild.__table_is_implicit__ is False
    assert AbstractBase.__table__ == AbstractChild.__table__ == ""
    assert AbstractBase.__table_is_implicit__ is False
    assert AbstractChild.__table_is_implicit__ is False
    assert ConcreteChild.__table__ == "modelnamingtest_concrete_childs"
    assert ConcreteChild.__table_is_implicit__ is True
    assert "__table_is_implicit__" not in {
        item.name for item in dataclasses.fields(InferredChild)
    }
    assert "__table_is_implicit__" not in state_of([InferredChild])


def test_table_state_rejects_duplicate_and_ambiguous_owners() -> None:
    class Invoice(Model):
        id: int = field(primary_key=True)

    class ExplicitInvoice(Model, table="modelnamingtest_invoices"):
        id: int = field(primary_key=True)

    class MixedCase(Model, table="Modelnamingtest_Invoices"):
        id: int = field(primary_key=True)

    with pytest.raises(MigrationError, match="both claim"):
        state_of([Invoice, ExplicitInvoice])
    with pytest.raises(MigrationError, match="both claim"):
        state_of([Invoice, MixedCase])
    assert set(state_of([Invoice, Invoice])["tables"]) == {"modelnamingtest_invoices"}
    assert set(state_of([Invoice], reference_models=[Invoice])["tables"]) == {
        "modelnamingtest_invoices"
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

    Derived = declare_model("Migration", app="hinoki")
    monkeypatch.setattr(ProjectManifest, "models", lambda _self, _app: [Derived])
    with pytest.raises(ProjectError, match="derived table 'hinoki_migrations'"):
        manifest.all_models()


def test_admin_table_ownership_is_separate_from_slug_and_same_model_aliases_work() -> (
    None
):
    class Invoice(Model):
        id: int = field(primary_key=True)

    class ExplicitInvoice(Model, table="Modelnamingtest_Invoices"):
        id: int = field(primary_key=True)

    admin = Admin(secret="s" * 32)
    admin.register(Invoice, slug="invoice")
    with pytest.raises(RegistrationError, match="conflicting table names"):
        admin.register(ExplicitInvoice, slug="legacy-invoice")

    aliases = Admin(secret="s" * 32)
    aliases.register(Invoice, slug="invoice")
    aliases.register(Invoice, slug="invoice-alias")
    assert set(aliases.models) == {"invoice", "invoice-alias"}
    with pytest.raises(RegistrationError, match="derived table"):
        aliases.register(declare_model("Cache", app="sqlite"), slug="cache")


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


@pytest.mark.parametrize(
    ("module", "app"),
    [
        ("shop.apps.billing.models", "billing"),
        ("shop.models", "shop"),
        ("shop.models.invoice", "shop"),
        ("shop.billing", "billing"),
        ("tests.modelnamingtest", "modelnamingtest"),
        ("__main__", "main"),
        ("Shop-Core.Models", "shop_core"),
        ("models", "models"),
        ("", ""),
    ],
)
def test_the_app_of_a_model_comes_from_the_package_of_its_models_module(
    module: str, app: str
) -> None:
    assert app_label(module) == app


def test_implicit_table_is_the_app_and_the_plural_model_name() -> None:
    Invoice = declare_model("Invoice", app="billing")
    LineItem = declare_model("LineItem", app="billing")

    assert Invoice.__table__ == "billing_invoices"
    assert LineItem.__table__ == "billing_line_items"
    assert (Invoice.__table_is_implicit__, LineItem.__table_is_implicit__) == (
        True,
        True,
    )


def test_the_app_option_is_checked_and_does_not_override_an_explicit_table() -> None:
    pinned = declare_model("Invoice", app="billing", table="old_invoices")
    assert pinned.__table__ == "old_invoices"
    assert pinned.__table_is_implicit__ is False
    for bad in ("Billing", "1billing", "bill-ing"):
        with pytest.raises(RegistrationError, match="app="):
            declare_model("Invoice", app=bad)
