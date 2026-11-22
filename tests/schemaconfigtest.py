"""Typed Schema configuration and unknown wire-field policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from hinoki import (
    UNSET,
    Field,
    Hinoki,
    Schema,
    SchemaConfig,
    SchemaExtra,
    Unset,
    http,
)
from hinoki.errors import RegistrationError, ValidationError
from hinoki.testing import ASGIClient


class VendorPayload(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    count: int


class StrictChild(Schema):
    value: str


class IgnoreChild(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    value: str


class IgnoreParent(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    child: StrictChild


class StrictParent(Schema):
    child: IgnoreChild


class IgnoreEvent(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    kind: Annotated[str, Field(one_of=("ignore",))]
    value: int


class StrictEvent(Schema):
    kind: Annotated[str, Field(one_of=("strict",))]
    value: str


class UnionPayload(Schema):
    event: Annotated[IgnoreEvent | StrictEvent, Field(discriminator="kind")]
    events: list[IgnoreEvent]


class AliasedInput(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    given_name: Annotated[str, Field(alias="givenName")]
    nickname: Annotated[str | Unset, Field(alias="nickName")] = UNSET


class IdentityAliasedInput(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    value: Annotated[str, Field(alias="value")]


class ConfigField(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    config: str


class BadKey:
    def __hash__(self) -> int:
        return 0

    def __str__(self) -> str:
        raise AssertionError("validation must not stringify non-string keys")

    def __repr__(self) -> str:
        raise AssertionError("validation must not repr non-string keys")


class BadValue:
    def __str__(self) -> str:
        raise AssertionError("ignored values must not be stringified")

    def __repr__(self) -> str:
        raise AssertionError("ignored values must not be repr'd")


class DiscriminatorCollidingKey:
    def __hash__(self) -> int:
        return hash("kind")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("malformed keys must not be compared")


handler_calls: list[str] = []


@http.post("/config-sync")
def config_sync(data: VendorPayload) -> dict[str, int]:
    handler_calls.append("sync")
    return {"count": data.count}


@http.post("/config-async")
async def config_async(data: VendorPayload) -> dict[str, int]:
    handler_calls.append("async")
    return {"count": data.count}


@http.post("/config-alias")
def config_alias(data: AliasedInput) -> dict[str, str]:
    return {"givenName": data.given_name}


@http.post("/config-identity-alias")
def config_identity_alias(data: IdentityAliasedInput) -> dict[str, str]:
    return {"value": data.value}


def make_app() -> Hinoki:
    app = Hinoki()
    app.include(config_sync, config_async, config_alias, config_identity_alias)
    return app


def test_extra_policy_is_typed_immutable_and_defaults_to_forbid() -> None:
    default = SchemaConfig()
    explicit = SchemaConfig(extra=SchemaExtra.FORBID)
    assert default == explicit
    with pytest.raises((AttributeError, TypeError)):
        default.extra = SchemaExtra.IGNORE  # type: ignore[misc]
    with pytest.raises(TypeError, match="extra must be a SchemaExtra"):
        SchemaConfig(extra="ignore")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="extra must be a SchemaExtra"):
        SchemaConfig(extra=None)  # type: ignore[arg-type]

    class DefaultForbid(Schema):
        value: str

    class ExplicitForbid(Schema, config=explicit):
        value: str

    for schema in (DefaultForbid, ExplicitForbid):
        with pytest.raises(ValidationError) as rejected:
            schema.validate({"value": "ok", "extra": 1})
        assert [(item.path, item.code) for item in rejected.value.details] == [
            ("extra", "extra_forbidden")
        ]


def test_ignore_discards_unknown_values_but_keeps_known_fields_strict() -> None:
    parsed = VendorPayload.validate({"count": 3, "vendor_context": BadValue()})
    assert parsed.count == 3
    assert parsed.dump() == {"count": 3}
    assert not hasattr(parsed, "vendor_context")

    with pytest.raises(ValidationError) as rejected:
        VendorPayload.validate({"count": "3", "vendor_context": "ignored"})
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("count", "type")
    ]
    with pytest.raises(ValidationError) as missing:
        VendorPayload.validate({"vendor_context": "ignored"})
    assert [(item.path, item.code) for item in missing.value.details] == [
        ("count", "required")
    ]

    # Config affects decoded wire input only: constructor keywords and explicit
    # model projection overrides keep their ordinary strict signatures.
    with pytest.raises(TypeError, match="unexpected keyword"):
        VendorPayload(count=1, vendor_context=2)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="has no fields"):
        VendorPayload.from_model(type("Row", (), {"count": 1})(), typo=2)


def test_non_string_mapping_keys_are_rejected_without_stringification() -> None:
    for schema in (VendorPayload, StrictParent):
        payload: dict[object, Any] = {BadKey(): "ignored"}
        if schema is VendorPayload:
            payload["count"] = 1
        else:
            payload["child"] = {"value": "ok"}
        with pytest.raises(ValidationError) as rejected:
            schema.validate(payload)
        assert [
            (item.path, item.code, item.message) for item in rejected.value.details
        ] == [("", "type", "object keys must be strings")]


def test_non_string_discriminator_keys_are_rejected_before_dict_lookup() -> None:
    with pytest.raises(ValidationError) as rejected:
        UnionPayload.validate({"event": {DiscriminatorCollidingKey(): 0}, "events": []})
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("event", "type")
    ]


def test_aliases_remain_canonical_and_python_names_are_unknown_extras() -> None:
    parsed = AliasedInput.validate({"givenName": "Aki", "extra": "ignored"})
    assert parsed.given_name == "Aki"
    assert parsed.nickname is UNSET
    assert parsed.dump() == {"givenName": "Aki"}

    with pytest.raises(ValidationError) as required:
        AliasedInput.validate({"given_name": "Aki"})
    assert [(item.path, item.code) for item in required.value.details] == [
        ("givenName", "required")
    ]
    with pytest.raises(ValidationError) as duplicate:
        AliasedInput.validate({"givenName": "Aki", "given_name": "Aki"})
    assert (duplicate.value.details[0].path, duplicate.value.details[0].code) == (
        "givenName",
        "duplicate_field",
    )


def test_nested_schemas_apply_their_own_extra_policy() -> None:
    parsed = StrictParent.validate({"child": {"value": "ok", "vendor": BadValue()}})
    assert parsed.child.value == "ok"

    with pytest.raises(ValidationError) as rejected:
        IgnoreParent.validate(
            {"child": {"value": "ok", "vendor": "ignored"}, "extra": 1}
        )
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("child.vendor", "extra_forbidden")
    ]


def test_lists_and_discriminated_variants_keep_their_own_policies() -> None:
    parsed = UnionPayload.validate(
        {
            "event": {"kind": "ignore", "value": 1, "extra": BadValue()},
            "events": [{"kind": "ignore", "value": 2, "extra": "ignored"}],
        }
    )
    assert parsed.event.value == 1
    assert parsed.events[0].value == 2

    with pytest.raises(ValidationError) as rejected:
        UnionPayload.validate(
            {
                "event": {"kind": "strict", "value": "ok", "extra": "refused"},
                "events": [],
            }
        )
    assert [(item.path, item.code) for item in rejected.value.details] == [
        ("event.extra", "extra_forbidden")
    ]


def test_config_inheritance_requires_explicit_resolution_for_conflicts() -> None:
    class ForbidBase(Schema, slots=False):
        left: str

    class IgnoreBase(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
        right: str

    class Inherited(IgnoreBase):
        value: str

    class Reset(IgnoreBase, config=SchemaConfig()):
        value: str

    assert Inherited.__schema_config__.extra is SchemaExtra.IGNORE
    assert Reset.__schema_config__.extra is SchemaExtra.FORBID

    with pytest.raises(RegistrationError, match="conflicting configs"):

        class Conflict(ForbidBase, IgnoreBase):
            pass

    class Resolved(ForbidBase, IgnoreBase, config=SchemaConfig()):
        value: str

    with pytest.raises(ValidationError):
        Resolved.validate({"left": "l", "right": "r", "value": "v", "x": 1})


def test_invalid_class_config_reserved_metadata_and_config_field() -> None:
    with pytest.raises(RegistrationError, match="config must be a SchemaConfig"):

        class InvalidConfig(Schema, config={"extra": "ignore"}):  # type: ignore[arg-type]
            value: str

    with pytest.raises(RegistrationError, match="config must be a SchemaConfig"):

        class NoneConfig(Schema, config=None):  # type: ignore[arg-type]
            value: str

    with pytest.raises(RegistrationError, match="reserved"):

        class Reserved(Schema):
            __schema_config__: str  # type: ignore[misc,assignment]

    assert ConfigField(config="data").config == "data"
    assert ConfigField.__schema_config__.extra is SchemaExtra.IGNORE


def test_schema_config_name_cannot_arrive_through_a_mixin() -> None:
    @dataclass(frozen=True)
    class ConfigMixin:
        __schema_config__: str

    with pytest.raises(RegistrationError, match="reserved"):

        class Combined(Schema, ConfigMixin):
            value: str


async def test_sync_and_async_request_handlers_receive_discarded_extra_input() -> None:
    handler_calls.clear()
    async with ASGIClient(make_app()) as client:
        sync = await client.post("/config-sync", json={"count": 4, "vendor": "ignored"})
        assert (sync.status, sync.body) == (200, {"count": 4})
        async_reply = await client.post(
            "/config-async", json={"count": 5, "vendor": "ignored"}
        )
        assert (async_reply.status, async_reply.body) == (200, {"count": 5})
        invalid = await client.post(
            "/config-async", json={"count": "5", "vendor": "ignored"}
        )
        assert invalid.status == 422
    assert handler_calls == ["sync", "async"]


class DefaultSchema(Schema):
    value: str


@http.get("/config-default")
def config_default() -> DefaultSchema:
    return DefaultSchema(value="ok")


def test_openapi_additional_properties_matches_each_schema_policy() -> None:
    app = make_app()
    app.include(config_default)
    document = app.openapi()
    schemas = document["components"]["schemas"]
    assert schemas["VendorPayload"]["additionalProperties"] is True
    assert schemas["DefaultSchema"]["additionalProperties"] is False


def test_openapi_ignore_schema_rejects_alias_and_python_name_together() -> None:
    document = make_app().openapi()
    schema = document["components"]["schemas"]["AliasedInput"]
    validator = Draft202012Validator(schema)

    assert not list(validator.iter_errors({"givenName": "Aki", "extra": 1}))
    assert list(validator.iter_errors({"givenName": "Aki", "given_name": "Aki"}))
    assert list(validator.iter_errors({"given_name": "Aki"}))
    # Optional Python names are unknown extras and may be discarded.
    assert not list(validator.iter_errors({"givenName": "Aki", "nickname": "A"}))

    identity_schema = document["components"]["schemas"]["IdentityAliasedInput"]
    Draft202012Validator.check_schema(identity_schema)
    assert not list(Draft202012Validator(identity_schema).iter_errors({"value": "ok"}))
