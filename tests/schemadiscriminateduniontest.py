"""Discriminated Schema unions use Rust dispatch and matching OpenAPI metadata."""

from types import SimpleNamespace
from typing import Annotated, ClassVar

import pytest

from hinoki import Field, Hinoki, Invalid, Schema, Unset, field_check, fieldtypes, http
from hinoki.errors import RegistrationError, ValidationError
from hinoki.fieldtypes import (
    ensure_variant_type,
    parse_annotation,
    validate_union_metadata,
)
from hinoki.testing import ASGIClient

variant_checks: list[str] = []
handler_calls: list[str] = []


class Cat(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("cat",))]
    name: Annotated[str, Field(alias="displayName", min_length=2)]
    checks: ClassVar[list[str]] = variant_checks

    @field_check("name")
    def record_name(self, value: str) -> None:
        self.checks.append(value)
        if value == "blocked":
            raise Invalid("displayName", "name is blocked")


class Dog(Schema):
    kind: Annotated[str, Field(alias="type", one_of=("dog",))]
    walks: int


PetUnion = Annotated[Cat | Dog, Field(discriminator="kind")]


class PetEnvelope(Schema):
    pet: PetUnion


class PetCollection(Schema):
    pets: list[PetUnion]


class OptionalPet(Schema):
    pet: Annotated[Cat | Dog | None, Field(discriminator="kind")] = None


class PatchPet(Schema):
    pet: Annotated[Cat | Dog | None, Field(discriminator="kind")]


class AssignedPet(Schema):
    pet: Cat | Dog = Field(discriminator="kind")


class DefaultPet(Schema):
    pet: PetUnion = Field(default_factory=lambda: Cat(kind="cat", name="Mugi"))


class UnrecognizedMetadata(Schema):
    value: Annotated[str, "ignored metadata"]


@http.post("/union/async")
async def async_union(data: PetEnvelope) -> PetEnvelope:
    handler_calls.append("async")
    return data


@http.post("/union/sync")
def sync_union(data: PetEnvelope) -> PetEnvelope:
    handler_calls.append("sync")
    return data


def test_wire_and_manual_validation_select_exact_variant() -> None:
    variant_checks.clear()
    cat = PetEnvelope.validate({"pet": {"type": "cat", "displayName": "Mugi"}})
    assert type(cat.pet) is Cat
    assert cat.dump() == {"pet": {"type": "cat", "displayName": "Mugi"}}
    assert cat.dump(by_alias=False) == {"pet": {"kind": "cat", "name": "Mugi"}}
    assert variant_checks == ["Mugi"]

    dog = PetEnvelope(pet=Dog(kind="dog", walks=3))
    assert type(dog.pet) is Dog
    assert dog.dump() == {"pet": {"type": "dog", "walks": 3}}
    assert PetEnvelope.from_model(SimpleNamespace(pet=dog.pet)) == dog
    cat_instance = Cat(kind="cat", name="Tama")
    variant_checks.clear()
    assert PetEnvelope.from_model(SimpleNamespace(pet=cat_instance)).pet is cat_instance
    assert variant_checks == ["Tama"]
    assert AssignedPet(pet=Dog(kind="dog", walks=5)).dump() == {
        "pet": {"type": "dog", "walks": 5}
    }
    assert DefaultPet().dump() == {"pet": {"type": "cat", "displayName": "Mugi"}}

    with pytest.raises(ValidationError) as wrong_python_type:
        PetEnvelope(pet={"type": "cat", "displayName": "Mugi"})  # type: ignore[arg-type]
    assert [(item.path, item.code) for item in wrong_python_type.value.details] == [
        ("pet", "type")
    ]

    changed = Cat(kind="cat", name="Mugi")
    object.__setattr__(changed, "kind", "dog")
    with pytest.raises(ValidationError) as mismatched:
        PetEnvelope(pet=changed)
    assert [(item.path, item.code) for item in mismatched.value.details] == [
        ("pet.kind", "discriminator_mismatch")
    ]


def test_union_errors_are_bounded_private_and_alias_aware() -> None:
    invalid_cases = [
        ({}, "discriminator_required", "pet.type"),
        ({"type": 2}, "discriminator_type", "pet.type"),
        ({"type": "other"}, "discriminator_unknown", "pet.type"),
        ({"type": "cat", "displayName": "x"}, "min_length", "pet.displayName"),
        (
            {"type": "cat", "kind": "dog", "displayName": "Mugi"},
            "duplicate_field",
            "pet.type",
        ),
        (
            {"type": "cat", "displayName": "blocked"},
            "displayName",
            "pet.displayName",
        ),
        (
            {"type": "dog", "walks": 1, "extra": True},
            "extra_forbidden",
            "pet.extra",
        ),
    ]
    for value, code, path in invalid_cases:
        with pytest.raises(ValidationError) as exc:
            PetEnvelope.validate({"pet": value})
        assert [(item.path, item.code) for item in exc.value.details] == [(path, code)]

    with pytest.raises(ValidationError) as no_echo:
        PetEnvelope.validate({"pet": {"type": "secret-user-value"}})
    assert "secret-user-value" not in str(no_echo.value)


def test_nested_lists_nullable_defaults_and_variant_checks() -> None:
    variant_checks.clear()
    collection = PetCollection.validate(
        {"pets": [{"type": "dog", "walks": 1}, {"type": "cat", "displayName": "Mugi"}]}
    )
    assert [type(item) for item in collection.pets] == [Dog, Cat]
    assert variant_checks == ["Mugi"]
    with pytest.raises(ValidationError) as indexed:
        PetCollection.validate({"pets": [{"type": "cat", "displayName": "x"}]})
    assert indexed.value.details[0].path == "pets[0].displayName"

    optional = OptionalPet()
    assert optional.pet is None
    assert optional.dump() == {"pet": None}
    patch = PatchPet.validate({"pet": None})
    assert patch.pet is None
    with pytest.raises(ValidationError) as missing:
        PatchPet.validate({})
    assert missing.value.details[0].code == "required"


@pytest.mark.asyncio
async def test_request_validation_precedes_sync_and_async_handlers() -> None:
    handler_calls.clear()
    app = Hinoki()
    app.include(async_union, sync_union)
    async with ASGIClient(app) as client:
        for path in ("/union/async", "/union/sync"):
            handler_calls.clear()
            bad = await client.post(
                path, json={"pet": {"type": "cat", "displayName": "x"}}
            )
            assert bad.status == 422
            assert bad.body["details"][0]["path"] == "data.pet.displayName"
            assert handler_calls == []
            good = await client.post(path, json={"pet": {"type": "dog", "walks": 4}})
            assert good.status == 200
            assert handler_calls[-1] == path.rsplit("/", 1)[-1]


def test_openapi_documents_tagged_one_of_for_objects_and_lists() -> None:
    app = Hinoki()
    app.include(sync_union)
    document = app.openapi()
    envelope = document["components"]["schemas"]["PetEnvelope"]
    union_schema = envelope["properties"]["pet"]
    assert union_schema == {
        "oneOf": [
            {"$ref": "#/components/schemas/Cat"},
            {"$ref": "#/components/schemas/Dog"},
        ],
        "discriminator": {
            "propertyName": "type",
            "mapping": {
                "cat": "#/components/schemas/Cat",
                "dog": "#/components/schemas/Dog",
            },
        },
    }
    variant = document["components"]["schemas"]["Cat"]
    assert variant["properties"]["type"] == {"enum": ["cat"], "type": "string"}
    assert variant["required"] == ["type", "displayName"]

    class Holder(Schema):
        pet: PetUnion
        pets: list[PetUnion]
        optional: Annotated[Cat | Dog | None, Field(discriminator="kind")] = None

    @http.get("/holder")
    def holder_route() -> Holder:
        return Holder(pet=Cat(kind="cat", name="Mugi"), pets=[])

    app.include(holder_route)
    nested = app.openapi()["components"]["schemas"]["Holder"]
    assert nested["properties"]["pets"]["items"] == union_schema
    assert nested["properties"]["optional"] == {
        "anyOf": [union_schema, {"type": "null"}]
    }


def declare_missing_union_metadata() -> None:
    class MissingMetadata(Schema):
        pet: Cat | Dog


def declare_missing_tag() -> None:
    class MissingTag(Schema):
        kind: str
        lives: int

    class OtherTag(Schema):
        kind: str
        walks: int

    class MissingTagUse(Schema):
        pet: Annotated[MissingTag | OtherTag, Field(discriminator="kind")]


def declare_mismatched_tag_aliases() -> None:
    class AliasTag(Schema):
        kind: Annotated[str, Field(alias="kind", one_of=("alias",))]

    class AliasTagOther(Schema):
        kind: Annotated[str, Field(alias="type", one_of=("other",))]

    class AliasTagUse(Schema):
        pet: Annotated[AliasTag | AliasTagOther, Field(discriminator="kind")]


def declare_custom_dump_variant() -> None:
    class CustomDumpVariant(Schema):
        kind: Annotated[str, Field(one_of=("custom",))]

        def dump(self) -> dict[str, object]:  # type: ignore[override]
            return {}

    class CustomDumpUse(Schema):
        item: Annotated[Cat | CustomDumpVariant, Field(discriminator="kind")]


def declare_scalar_discriminator() -> None:
    class BadScalarDiscriminator(Schema):
        value: Annotated[str, Field(discriminator="kind")]


def declare_conflicting_nested_aliases() -> None:
    class ConflictingAliases(Schema):
        value: Annotated[
            Annotated[str, Field(alias="inner")] | None,
            Field(alias="outer"),
        ]


def declare_conflicting_nested_discriminators() -> None:
    class First(Schema):
        kind: Annotated[str, Field(one_of=("first",))]

    class Second(Schema):
        kind: Annotated[str, Field(one_of=("second",))]

    class ConflictingDiscriminators(Schema):
        value: Annotated[
            Annotated[First | Second, Field(discriminator="kind")],
            Field(discriminator="other"),
        ]


def declare_duplicate_tags() -> None:
    class First(Schema):
        kind: Annotated[str, Field(one_of=("same",))]

    class Second(Schema):
        kind: Annotated[str, Field(one_of=("same",))]

    class DuplicateTags(Schema):
        value: Annotated[First | Second, Field(discriminator="kind")]


def declare_union_with_non_schema_member() -> None:
    class InvalidVariant(Schema):
        value: Annotated[Cat | int, Field(discriminator="kind")]


def declare_union_with_invalid_discriminator_name() -> None:
    class First(Schema):
        kind: Annotated[str, Field(one_of=("first",))]

    class Second(Schema):
        kind: Annotated[str, Field(one_of=("second",))]

    class InvalidDiscriminator(Schema):
        value: Annotated[First | Second, Field(discriminator="not-a-field")]


def declare_union_with_constraints() -> None:
    class First(Schema):
        kind: Annotated[str, Field(one_of=("first",))]

    class Second(Schema):
        kind: Annotated[str, Field(one_of=("second",))]

    class InvalidUnion(Schema):
        value: Annotated[
            First | Second,
            Field(discriminator="kind", min_length=1),
        ]


def declare_union_with_missing_discriminator_field() -> None:
    class First(Schema):
        tag: Annotated[str, Field(one_of=("first",))]

    class Second(Schema):
        tag: Annotated[str, Field(one_of=("second",))]

    class MissingField(Schema):
        value: Annotated[First | Second, Field(discriminator="kind")]


def test_invalid_discriminator_declarations_fail_at_schema_registration() -> None:
    with pytest.raises(RegistrationError, match=r"Field\(discriminator"):
        declare_missing_union_metadata()
    with pytest.raises(RegistrationError, match="must be a required string"):
        declare_missing_tag()
    with pytest.raises(RegistrationError, match="aliases must match"):
        declare_mismatched_tag_aliases()
    with pytest.raises(RegistrationError, match="cannot override dump"):
        declare_custom_dump_variant()
    with pytest.raises(RegistrationError, match="only valid on a union"):
        declare_scalar_discriminator()
    with pytest.raises(RegistrationError, match="conflicting aliases"):
        declare_conflicting_nested_aliases()
    with pytest.raises(RegistrationError, match="conflicting discriminators"):
        declare_conflicting_nested_discriminators()
    with pytest.raises(RegistrationError, match="duplicate discriminator tag"):
        declare_duplicate_tags()
    with pytest.raises(RegistrationError, match="must be Schema classes"):
        declare_union_with_non_schema_member()
    with pytest.raises(RegistrationError, match="Python field name"):
        declare_union_with_invalid_discriminator_name()
    with pytest.raises(RegistrationError, match="accepts only discriminator"):
        declare_union_with_constraints()
    with pytest.raises(RegistrationError, match="needs discriminator field"):
        declare_union_with_missing_discriminator_field()


def test_union_without_a_member_is_rejected() -> None:
    with pytest.raises(RegistrationError, match="2 to 16 variants"):
        validate_union_metadata(Field(discriminator="kind"), [Cat], "empty")


def test_schema_fieldtype_helpers_reject_empty_unions_and_repeated_classes() -> None:
    with pytest.raises(RegistrationError, match="unsupported union"):
        parse_annotation(type(None) | Unset, "empty")
    with pytest.raises(RegistrationError, match="duplicate Schema union variant"):
        ensure_variant_type(Cat, Schema, [("cat", Cat)], "repeated")


def test_schema_inheritance_is_not_a_discriminated_union() -> None:
    class SpecialCat(Cat):
        detail: str

    with pytest.raises(RegistrationError, match="cannot inherit from each other"):
        ensure_variant_type(SpecialCat, Schema, [("cat", Cat)], "related")


def test_union_helpers_keep_defensive_empty_and_missing_tag_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fieldtypes, "validate_union_metadata", lambda _info, _members, _where: "kind"
    )
    with pytest.raises(RegistrationError, match="union has no variants"):
        fieldtypes.build_tagged_union(
            [], Field(discriminator="kind"), False, False, "empty"
        )

    class Untagged(Schema):
        kind: str

    monkeypatch.setattr(fieldtypes, "invalid_discriminator_field", lambda *_args: False)
    with pytest.raises(RegistrationError, match="discriminator tag is missing"):
        fieldtypes.variant_discriminator(Untagged, "kind", "untagged")


def test_openapi_rejects_bare_union_return_annotations() -> None:
    @http.get("/bare-union")
    def bare_union() -> Cat | Dog:
        return Cat(kind="cat", name="Mugi")

    app = Hinoki()
    app.include(bare_union)
    with pytest.raises(RegistrationError, match="wrapper Schema"):
        app.openapi()
