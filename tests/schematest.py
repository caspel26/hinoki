from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import pytest

from hinoki import UNSET, Field, RegistrationError, Schema, Unset, ValidationError


class Author(Schema):
    id: UUID
    name: Annotated[str, Field(min_length=1)]


class PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=10)]
    slug: Annotated[str, Field(pattern=r"[a-z]+")]
    stars: Annotated[int, Field(ge=0, le=5)] = 0
    score: Annotated[float, Field(gt=0, lt=10)] = 1.0
    draft: bool = False
    tags: Annotated[list[str], Field(max_length=2)] = Field(default_factory=list)
    subtitle: str | None = None
    author: Author | None = None
    created: datetime | None = None


class PostPatch(Schema):
    title: str | Unset = UNSET
    subtitle: str | None | Unset = UNSET


class Extended(PostCreate):
    extra: int = 1


VALID: dict[str, Any] = {"title": "Hello", "slug": "hello"}
AUTHORED: dict[str, Any] = {"created": "2026-11-13T10:00:00Z"}


def codes(data: object, schema: type[Schema] = PostCreate) -> dict[str, str]:
    with pytest.raises(ValidationError) as info:
        schema.validate(data)
    return {d.path: d.code for d in info.value.details}


def test_valid_payload_builds_a_typed_instance_with_defaults() -> None:
    post = PostCreate.validate(VALID)
    assert post.title == "Hello"
    assert post.stars == 0
    assert post.tags == []
    assert post.subtitle is None


def test_defaults_are_not_shared_between_instances() -> None:
    first, second = PostCreate.validate(VALID), PostCreate.validate(VALID)
    assert first.tags is not second.tags


def test_instances_are_immutable() -> None:
    post = PostCreate.validate(VALID)
    with pytest.raises(AttributeError):
        post.title = "x"  # type: ignore[misc]


def test_nested_schema_datetime_and_uuid() -> None:
    uid = "6f1c8a52-0000-4000-8000-000000000001"
    post = PostCreate.validate(
        {
            **VALID,
            "author": {"id": uid, "name": "Ada"},
            "created": "2026-11-13T10:00:00Z",
        }
    )
    assert post.author is not None
    assert post.author.id == UUID(uid)
    assert post.created == datetime(2026, 11, 13, 10, tzinfo=UTC)


def test_every_error_is_reported_with_its_path_and_a_stable_code() -> None:
    found = codes(
        {
            "title": "",
            "slug": "UPPER",
            "stars": 9,
            "score": 0,
            "tags": ["a", 2, "c"],
            "author": {"id": "nope"},
            "created": "2026-11-13T10:00:00",
            "extra": 1,
        }
    )
    assert found == {
        "title": "min_length",
        "slug": "pattern",
        "stars": "le",
        "score": "gt",
        "tags": "max_length",
        "author.id": "uuid",
        "author.name": "required",
        "created": "datetime_tz",
        "extra": "extra_forbidden",
    }


def test_list_item_errors_carry_the_index() -> None:
    assert codes({**VALID, "tags": ["a", 2]}) == {"tags[1]": "type"}


def test_missing_required_fields() -> None:
    assert codes({}) == {"title": "required", "slug": "required"}


@pytest.mark.parametrize(
    ("field", "value"),
    [("stars", "3"), ("stars", True), ("stars", 1.5), ("draft", 1), ("title", 5)],
)
def test_no_implicit_coercion(field: str, value: object) -> None:
    assert codes({**VALID, field: value}) == {field: "type"}


def test_int_is_accepted_for_float_but_bool_is_not() -> None:
    assert PostCreate.validate({**VALID, "score": 3}).score == 3.0
    assert codes({**VALID, "score": True}) == {"score": "type"}


def test_null_only_where_nullable() -> None:
    assert PostCreate.validate({**VALID, "subtitle": None}).subtitle is None
    assert codes({**VALID, "title": None}) == {"title": "null"}


def test_non_object_input_is_rejected() -> None:
    assert codes([1]) == {"": "type"}


def test_patch_distinguishes_absent_from_null() -> None:
    absent = PostPatch.validate({})
    assert absent.subtitle is UNSET
    assert PostPatch.validate({"subtitle": None}).subtitle is None
    assert codes({"title": None}, PostPatch) == {"title": "null"}
    assert absent.dump() == {}


def test_dump_is_json_compatible() -> None:
    uid = "6f1c8a52-0000-4000-8000-000000000001"
    post = PostCreate.validate(
        {
            **VALID,
            "author": {"id": uid, "name": "Ada"},
            "created": "2026-11-13T10:00:00Z",
        }
    )
    dumped = post.dump()
    assert dumped["author"] == {"id": uid, "name": "Ada"}
    assert dumped["created"] == "2026-11-13T10:00:00+00:00"
    assert dumped["tags"] == []


def test_fields_introspection_describes_requirements() -> None:
    fields = PostCreate.fields()
    assert fields["title"].required
    assert not fields["stars"].required
    assert fields["subtitle"].ftype.nullable
    assert fields["title"].ftype.info.max_length == 10


def test_inheritance_extends_fields() -> None:
    assert "extra" in Extended.fields()
    assert Extended.validate(VALID).extra == 1


UNSUPPORTED: list[Any] = [dict[str, int], set[int], bytes, int | str]


@pytest.mark.parametrize("annotation", UNSUPPORTED)
def test_unsupported_types_fail_at_class_creation(annotation: Any) -> None:
    with pytest.raises(RegistrationError):
        type("Bad", (Schema,), {"__annotations__": {"value": annotation}})


def test_constraints_must_fit_the_type() -> None:
    with pytest.raises(RegistrationError, match="do not apply"):
        type(
            "Bad",
            (Schema,),
            {"__annotations__": {"value": Annotated[int, Field(pattern="x")]}},
        )
