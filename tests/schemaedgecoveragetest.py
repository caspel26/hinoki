"""Boundary cases for Schema normalization and model conversion helpers."""

from __future__ import annotations

import typing
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

import pytest

from hinoki import (
    UNSET,
    Field,
    Invalid,
    Schema,
    Unset,
    field_check,
    field_normalize,
)
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fieldtypes import FieldType
from hinoki.schema import (
    _INVALID_INSTANCE_VALUE,
    _MAX_INSTANCE_DEPTH,
    _TRUSTED_CONSTRUCTOR,
    FieldSpec,
    allocate_schema_instance,
    annotation_is_str,
    chosen_columns,
    encode,
    encode_items,
    from_row,
    instance_error,
    list_instance_value,
    merge_alias,
    nested_instance_value,
    normalize_selector,
    plain,
    prepare_normalized_values,
    resolve_schema_config,
    run_checks,
    schema_instance_values,
    stop_awaitable,
    tagged_union_instance_value,
    typed_instance_value,
    uses_generated_init,
    validate_aliases,
    validate_normalizer_method,
    validate_reserved_names,
    walk_model_schema,
    walk_model_value,
)
from hinoki.schemaconfig import SchemaConfig, SchemaExtra


class Child(Schema):
    value: str


class Parent(Schema):
    child: Child


class Recursive(Schema):
    child: Recursive | None = None


class Item(Schema):
    name: str


class Items(Schema):
    children: list[Item]


class Cat(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("cat",))]


class Dog(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("dog",))]


class Pet(Schema):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]


def test_field_normalizer_mark_and_annotation_contracts() -> None:
    with pytest.raises(RegistrationError, match="non-empty"):
        field_normalize("")
    assert annotation_is_str(Annotated[str, Field(min_length=1)])
    assert not annotation_is_str(Annotated[int, Field(ge=0)])
    with pytest.raises(RegistrationError, match="at least one field"):
        field_check()
    with pytest.raises(RegistrationError, match="conflicting aliases"):
        merge_alias("Schema", "name", "left", "right")

    class InvalidMethods(Schema):
        value: str

        @staticmethod
        def static(value: str) -> str:
            return value

        async def asynchronous(self, value: str) -> str:
            return value

        def extra(self, value: str, other: str) -> str:
            return value + other

    for name, message in (
        ("static", "instance methods"),
        ("asynchronous", "sync methods"),
        ("extra", "expected"),
    ):
        with pytest.raises(RegistrationError, match=message):
            validate_normalizer_method(InvalidMethods, name)


def test_instance_export_skips_missing_and_rejects_cycles_and_depth() -> None:
    sparse = object.__new__(Parent)
    assert schema_instance_values(Parent, sparse) == {}

    recursive = Recursive()
    object.__setattr__(recursive, "child", recursive)
    with pytest.raises(ValidationError) as error:
        schema_instance_values(Recursive, recursive)
    assert error.value.details[0].message == "input is nested too deeply"
    with pytest.raises(ValidationError) as error:
        schema_instance_values(
            Parent, Parent(child=Child(value="x")), depth=_MAX_INSTANCE_DEPTH + 1
        )
    assert error.value.details[0].message == "input is nested too deeply"
    assert instance_error("child").details[0].path == "child"


def test_list_instance_conversion_preserves_nonlists_and_bounds_nesting() -> None:
    ftype = Parent.specs()["child"].ftype
    marker = ("not", "a", "list")
    assert list_instance_value(ftype, marker, False, set(), 0, "child") is marker
    with pytest.raises(ValidationError) as error:
        list_instance_value(ftype, [], False, set(), _MAX_INSTANCE_DEPTH + 1, "child")
    assert error.value.details[0].message == "input is nested too deeply"


def test_plain_and_row_conversion_keep_scalar_shape_and_convert_nested_models() -> None:
    class Row:
        __table__ = object()

        def __init__(self, name: str) -> None:
            self.name = name

    class Output(Schema):
        item: Item

    value = {"nested": (Item(name="a"), [Item(name="b")])}
    assert plain(value) == {"nested": [{"name": "a"}, [{"name": "b"}]]}
    assert from_row(FieldType("str"), None) is None
    item_type = Output.specs()["item"].ftype
    converted = from_row(item_type, Row("row"))
    assert converted == Item(name="row")
    assert from_row(item_type, "plain") == "plain"


def test_tagged_union_instance_conversion_checks_variants_and_discriminator() -> None:
    field_type = Pet.specs()["pet"].ftype
    active: set[int] = set()
    assert (
        tagged_union_instance_value(field_type, "cat", False, active, 0, "pet")
        is _INVALID_INSTANCE_VALUE
    )
    assert (
        tagged_union_instance_value(
            field_type, Dog(kind="dog"), False, active, 0, "pet"
        )
        is not _INVALID_INSTANCE_VALUE
    )
    wrong_tag = Cat(kind="cat")
    object.__setattr__(wrong_tag, "kind", "dog")
    with pytest.raises(ValidationError) as error:
        tagged_union_instance_value(field_type, wrong_tag, True, active, 0, "pet")
    assert error.value.details[0].path == "pet.type"
    assert (
        tagged_union_instance_value(
            replace(field_type, discriminator=None),
            Cat(kind="cat"),
            False,
            set(),
            0,
            "pet",
        )
        is _INVALID_INSTANCE_VALUE
    )
    assert (
        tagged_union_instance_value(
            replace(field_type, discriminator_wire_name=None),
            Cat(kind="cat"),
            False,
            set(),
            0,
            "pet",
        )
        is _INVALID_INSTANCE_VALUE
    )


def test_conversion_and_selector_helpers_reject_bad_shape_inputs() -> None:
    assert (
        nested_instance_value(Child, "not a schema", False, set(), 0, "child")
        is _INVALID_INSTANCE_VALUE
    )
    specs = Parent.specs()
    with pytest.raises(TypeError, match="iterable"):
        normalize_selector(4, "include", specs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="iterable"):
        normalize_selector(None, "exclude", specs)
    assert normalize_selector(None, "include", specs) is None
    with pytest.raises(TypeError, match="not str"):
        normalize_selector("child", "include", specs)
    with pytest.raises(TypeError, match="members must be strings"):
        normalize_selector(("child", 1), "include", specs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown include fields"):
        normalize_selector(("absent",), "include", specs)

    class ModelColumns:
        __columns__ = ("id", "name")
        __name__ = "ModelColumns"

    assert chosen_columns(Parent, ModelColumns, None, (), True) == (
        ["id", "name"],
        set(),
        True,
    )
    with pytest.raises(RegistrationError, match="fields= or exclude="):
        chosen_columns(Parent, ModelColumns, ("id",), ("name",), False)
    with pytest.raises(RegistrationError, match="no columns"):
        chosen_columns(Parent, ModelColumns, ("missing",), (), False)
    with pytest.raises(RegistrationError, match="optional names"):
        chosen_columns(Parent, ModelColumns, ("id",), (), ("name",))


def test_normalizer_empty_proposals_and_awaitable_cleanup() -> None:
    class OptionalText(Schema):
        value: str | None = None

        @field_normalize("value")
        def canonicalize(self, value: str) -> str:
            return value.strip()

    assert prepare_normalized_values(OptionalText(), True, "") == {}

    class InvalidText(Schema):
        value: str

        @field_normalize("value")
        def canonicalize(self, value: str) -> str:
            raise Invalid("reject", "cannot normalize")

    with pytest.raises(TypeError, match="use @field_check"):
        InvalidText(value="x")

    async def canonicalize(value: str) -> str:
        return value

    class AwaitableText(Schema):
        value: str

        @field_normalize("value")
        def normalize(self, value: str) -> str:
            return canonicalize(value)  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must be synchronous"):
        AwaitableText(value="x")

    class NonString(Schema):
        value: str

        @field_normalize("value")
        def normalize(self, value: str) -> str:
            return None  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must return a string"):
        NonString(value="x")

    class Cleanup:
        closed = False
        cancelled = False

        def close(self) -> None:
            self.closed = True

        def cancel(self) -> None:
            self.cancelled = True

    cleanup = Cleanup()
    stop_awaitable(cleanup)
    assert cleanup.closed and cleanup.cancelled


def test_custom_allocator_receives_schema_values() -> None:
    class CustomNew(Schema):
        value: str

        def __new__(cls, *, value: str) -> CustomNew:
            instance = object.__new__(cls)
            object.__setattr__(instance, "allocated", value)
            return instance

    instance = allocate_schema_instance(CustomNew, {"value": "allocated"})
    assert vars(instance)["allocated"] == "allocated"


def test_schema_encoding_and_scalar_native_instance_values() -> None:
    identifier = uuid4()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert typed_instance_value(
        FieldType("uuid"),
        identifier,
        use_aliases=False,
        active=set(),
        depth=0,
        path="id",
    ) == str(identifier)
    assert (
        typed_instance_value(
            FieldType("uuid"),
            "invalid",
            use_aliases=False,
            active=set(),
            depth=0,
            path="id",
        )
        is _INVALID_INSTANCE_VALUE
    )
    assert (
        typed_instance_value(
            FieldType("datetime"),
            now,
            use_aliases=False,
            active=set(),
            depth=0,
            path="at",
        )
        == now.isoformat()
    )
    assert (
        typed_instance_value(
            FieldType("datetime"),
            "invalid",
            use_aliases=False,
            active=set(),
            depth=0,
            path="at",
        )
        is _INVALID_INSTANCE_VALUE
    )
    assert encode({"id": identifier, "at": [now]}) == {
        "id": str(identifier),
        "at": [now.isoformat()],
    }
    marker = object()
    assert encode(marker) is marker


def test_alias_validation_rejects_invalid_names_and_collisions() -> None:
    ftype = Parent.specs()["child"].ftype
    with pytest.raises(RegistrationError, match="cannot contain"):
        validate_aliases(Parent, {"child": FieldSpec("child", "a.b", ftype, True)})
    with pytest.raises(RegistrationError, match="share wire name"):
        validate_aliases(
            Parent,
            {
                "child": FieldSpec("child", "same", ftype, True),
                "other": FieldSpec("other", "same", ftype, True),
            },
        )


def test_trusted_custom_init_constructor_validates_application_init() -> None:
    class Custom(Schema):
        value: str

        def __init__(self, value: str) -> None:
            object.__setattr__(self, "value", value.strip())

    instance = getattr(Custom, _TRUSTED_CONSTRUCTOR)({"value": " valid "})
    assert instance.value == "valid"


def test_schema_configuration_and_reserved_inherited_names_are_checked() -> None:
    with pytest.raises(RegistrationError, match="config must be"):
        resolve_schema_config(Parent, object())  # type: ignore[arg-type]

    class Left(Schema):
        pass

    class Right(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
        pass

    with pytest.raises(RegistrationError, match="conflicting configs"):

        class Mixed(Left, Right):
            pass

    class NonSchemaBase:
        __annotations__ = {"__schema_config__": object}

    with pytest.raises(RegistrationError, match="tracking metadata is reserved"):
        validate_reserved_names(type("Child", (NonSchemaBase,), {}), {})


def test_forward_reference_failure_is_reported_on_first_schema_use() -> None:
    class Late(Schema):
        child: "MissingSchemaForCoverage"  # type: ignore[name-defined]  # noqa: F821, UP037

    with pytest.raises(RegistrationError, match="MissingSchemaForCoverage"):
        Late.specs()


def test_normalizer_annotation_resolution_errors_become_registration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Valid(Schema):
        value: str

        @field_normalize("value")
        def canonicalize(self, value: str) -> str:
            return value

    monkeypatch.setattr(
        typing,
        "get_type_hints",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(NameError("missing")),
    )
    with pytest.raises(RegistrationError, match="annotations must resolve"):
        validate_normalizer_method(Valid, "canonicalize")


def test_missing_union_variant_and_invalid_default_field_are_rejected() -> None:
    class Other(Schema):
        value: str

    assert (
        tagged_union_instance_value(
            Pet.specs()["pet"].ftype, Other(value="x"), False, set(), 0, "pet"
        )
        is _INVALID_INSTANCE_VALUE
    )

    def declare_invalid_default() -> None:
        class BadDefault(Schema):
            value: str = Field(min_length=1)  # type: ignore[assignment]

    with pytest.raises(RegistrationError, match="constraints in Field"):
        declare_invalid_default()


def test_graph_walk_and_json_encoder_cover_repeated_and_collection_values() -> None:
    node = Parent(child=Child(value="nested"))
    queue: list[tuple[Schema, bool, str]] = [(node, False, "old")]
    walk_model_schema(node, "new", queue, {id(node): 0}, set())
    assert queue[0] == (node, False, "new")
    walk_model_schema(node, "ignored", queue, {id(node): 0}, {id(node)})
    assert run_checks(node, "", seen={id(node)}) == []

    missing = object.__new__(Parent)
    walk_model_schema(missing, "root", [], {}, set())
    ftype = Pet.specs()["pet"].ftype
    walk_model_value(ftype, Cat(kind="cat"), "pet", [], {}, set())
    list_type = Items.specs()["children"].ftype
    walk_model_value(list_type, (Item(name="x"),), "items", [], {}, set())
    assert plain(1) == 1

    class Row:
        __table__ = object()

        def __init__(self, name: str) -> None:
            self.name = name

    assert from_row(list_type, (Row("x"),)) == [Item(name="x")]

    class Renderable:
        def dump(self) -> dict[str, object]:
            return {"value": 1}

    assert encode(Renderable()) == {"value": 1}
    identifier = uuid4()
    assert encode_items({"id": identifier}) == {"id": str(identifier)}


def test_generated_initializer_detection_handles_custom_static_allocator() -> None:
    class CustomAllocator(Schema):
        value: str

        def __new__(cls, **_values: object) -> CustomAllocator:
            return object.__new__(cls)

    assert not uses_generated_init(CustomAllocator)


def test_unset_nested_schema_values_do_not_create_exported_fields() -> None:
    class Patch(Schema):
        value: str | Unset = UNSET

    # The public Unset annotation is covered by Schema validation tests; here
    # verify the instance graph helper omits the sentinel for optional patches.
    instance = object.__new__(Patch)
    object.__setattr__(instance, "value", UNSET)
    assert schema_instance_values(Patch, instance) == {}
