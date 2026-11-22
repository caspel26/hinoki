"""Boundary cases for Schema normalization and model conversion helpers."""

from __future__ import annotations

import enum
import sys
import types
import typing
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import uuid4

import pytest

from hinoki import (
    UNSET,
    Field,
    Invalid,
    Schema,
    Unset,
    before_validate,
    field_check,
    field_normalize,
)
from hinoki import schema as schema_module
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fieldtypes import FieldType
from hinoki.schema import (
    _INVALID_INSTANCE_VALUE,
    _MAX_INSTANCE_DEPTH,
    _TRUSTED_CONSTRUCTOR,
    FieldSpec,
    _declared_annotations,
    _is_class_var,
    allocate_schema_instance,
    annotation_is_str,
    chosen_columns,
    encode,
    encode_items,
    fixed_instance_value,
    from_row,
    instance_error,
    list_instance_value,
    member_accepts,
    member_key,
    merge_alias,
    native_replace,
    nested_instance_value,
    normalize_selector,
    plain,
    prepare_normalized_values,
    prepare_value,
    resolve_schema_config,
    run_checks,
    schema_instance_values,
    sorted_items,
    stop_awaitable,
    tagged_union_instance_value,
    text_native,
    type_accepts,
    type_reaches_before,
    typed_entries,
    typed_instance_value,
    uses_generated_init,
    validate_aliases,
    validate_normalizer_method,
    validate_reserved_names,
    variant_accepts,
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


class Shade(enum.Enum):
    DARK = "dark"


class Hooked(Schema):
    kind: Annotated[str, Field(one_of=("hooked",))]
    name: str

    @before_validate
    @classmethod
    def trim(cls, data: dict[str, Any]) -> dict[str, Any]:
        return {**data, "name": str(data.get("name", "")).strip()}


class Plain(Schema):
    kind: Annotated[str, Field(one_of=("plain",))]


class Holder(Schema):
    pair: tuple[Hooked, int]
    union: Annotated[Hooked | Plain, Field(discriminator="kind")]
    choice: int | str
    anything: Any
    shade: Shade
    maybe: int | None
    tags: tuple[int, str]


def test_instance_checks_follow_each_kind_of_field_type() -> None:
    specs = Holder.specs()
    anything, maybe = specs["anything"].ftype, specs["maybe"].ftype
    assert variant_accepts(anything, object())
    assert variant_accepts(maybe, None)
    assert not variant_accepts(specs["choice"].ftype, None)
    assert type_accepts(specs["choice"].ftype, "x")
    assert not type_accepts(specs["choice"].ftype, 1.5)
    assert type_accepts(specs["union"].ftype, Plain(kind="plain"))
    assert not type_accepts(specs["union"].ftype, "plain")
    assert member_accepts(specs["shade"].ftype, Shade.DARK)
    assert not member_accepts(specs["shade"].ftype, "dark")


def test_a_fixed_tuple_instance_is_bounded_in_depth() -> None:
    ftype = Holder.specs()["tags"].ftype
    with pytest.raises(ValidationError) as error:
        fixed_instance_value(
            ftype, (1, "a"), False, set(), _MAX_INSTANCE_DEPTH + 1, "p"
        )
    assert error.value.details[0].message == "input is nested too deeply"


def test_before_hooks_reach_tuple_members_and_discriminated_variants() -> None:
    specs = Holder.specs()
    pair = prepare_value(
        specs["pair"].ftype, [{"kind": "hooked", "name": " a "}, 1], "", 0
    )
    assert pair == [{"kind": "hooked", "name": "a"}, 1]
    union = specs["union"].ftype
    assert prepare_value(union, {"kind": "hooked", "name": " b "}, "", 0) == {
        "kind": "hooked",
        "name": "b",
    }
    assert prepare_value(union, "text", "", 0) == "text"
    unknown = {"kind": "other", "name": " c "}
    assert prepare_value(union, unknown, "", 0) is unknown
    assert prepare_value(specs["pair"].ftype, "text", "", 0) == "text"
    assert type_reaches_before(union)
    assert not type_reaches_before(specs["choice"].ftype)


def test_a_plain_union_without_hooks_passes_input_through_the_hook_walk() -> None:
    ftype = Holder.specs()["choice"].ftype
    assert prepare_value(ftype, "x", "", 0) == "x"


def test_typed_entries_follow_plain_unions_and_fixed_tuples() -> None:
    specs = Holder.specs()
    choice = specs["choice"].ftype
    [(chosen, path, value)] = typed_entries(choice, "x", "f")
    assert (chosen.kind, path, value) == ("str", "f", "x")
    assert typed_entries(choice, 1.5, "f") == []
    entries = typed_entries(specs["tags"].ftype, (1, "a"), "t")
    assert [(path, item) for _, path, item in entries] == [("t[0]", 1), ("t[1]", "a")]
    assert typed_entries(specs["tags"].ftype, (1,), "t") == []
    assert typed_entries(specs["tags"].ftype, [1, "a"], "t") == []


def test_set_members_are_ordered_by_tuple_contents_or_kept_when_unsortable() -> None:
    assert member_key((2, 1)) == (3, (member_key(2), member_key(1)))
    assert sorted_items({(2, "b"), (1, "z")}) == [(1, "z"), (2, "b")]
    unorderable = {1j, 2j}
    assert sorted_items(unorderable) == list(unorderable)


def test_a_forward_reference_to_a_class_variable_is_not_a_field() -> None:
    assert _is_class_var(typing.ForwardRef("ClassVar[int]"))
    assert not _is_class_var(typing.ForwardRef("int"))


def test_lazy_annotations_are_read_through_annotationlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = types.ModuleType("annotationlib")
    fake.Format = types.SimpleNamespace(FORWARDREF="forwardref")  # type: ignore[attr-defined]
    fake.call_annotate_function = lambda annotate, fmt: {"x": (annotate, fmt)}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "annotationlib", fake)

    def annotate(format: int) -> dict[str, object]:
        return {}

    assert _declared_annotations({"__annotate__": annotate}) == {
        "x": (annotate, "forwardref")
    }
    assert _declared_annotations({}) == {}


def test_a_schema_with_a_duplicate_base_reports_the_python_error() -> None:
    with pytest.raises(TypeError, match="duplicate base class"):
        type("Twice", (Schema, Schema), {"__annotations__": {"value": int}})


def test_text_validation_stays_in_python_when_the_native_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Fresh(Schema):
        value: int

    def broken(_cls: object) -> object:
        raise RuntimeError("native build failed")

    monkeypatch.setattr(schema_module, "native_validator", broken)
    assert text_native(Fresh) is False


def test_a_replace_without_a_native_copy_uses_the_python_path() -> None:
    class Lacking:
        @classmethod
        def native_schema(cls) -> object:
            return object()

    assert native_replace(cast(Any, Lacking()), {"value": 2}) is None


def test_dumping_by_alias_with_an_empty_list_exclusion_matches_the_fast_dump() -> None:
    class Aliased(Schema):
        user_id: Annotated[int, Field(alias="userId")]

    item = Aliased(user_id=3)
    assert item.dump(by_alias=True, exclude=[]) == {"userId": 3}
