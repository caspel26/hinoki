"""Explicit Schema projections preserve caller presence and privacy boundaries."""

import inspect
import json
from copy import copy
from dataclasses import dataclass, replace
from pickle import dumps, loads
from typing import Annotated

import pytest

from hinoki import UNSET, Field, Schema, Unset, core
from hinoki.errors import RegistrationError


class Defaults(Schema):
    label: str = "default"
    amount: int = 0
    enabled: bool = False
    note: str | None = None
    items: list[str] = Field(default_factory=list)
    patch: str | Unset = UNSET


class Card(Schema):
    display_name: Annotated[str, Field(alias="displayName")]
    note: str = "new"


class Board(Schema):
    title: str
    cards: list[Card]


def test_exclude_unset_uses_constructor_presence_not_default_equality() -> None:
    empty = Defaults()
    explicit = Defaults(
        label="default", amount=0, enabled=False, note=None, items=[], patch=UNSET
    )

    assert empty.dump() == {
        "label": "default",
        "amount": 0,
        "enabled": False,
        "note": None,
        "items": [],
    }
    assert empty.serialize(exclude_unset=True) == {}
    assert empty.serialize() == empty.dump()
    assert explicit.serialize(exclude_unset=True) == {
        "label": "default",
        "amount": 0,
        "enabled": False,
        "note": None,
        "items": [],
    }


def test_validate_and_from_model_track_only_values_that_were_provided() -> None:
    validated = Defaults.validate({"amount": 0, "note": None})
    assert validated.serialize(exclude_unset=True) == {"amount": 0, "note": None}

    class Output(Schema):
        name: str
        label: str = "default"

    from_model = Output.from_model(type("Row", (), {"name": "Aki"})())
    assert from_model.serialize(exclude_unset=True) == {"name": "Aki"}
    assert Output.from_model(
        type("Row", (), {"name": "Aki", "label": "default"})()
    ).serialize(exclude_unset=True) == {"name": "Aki", "label": "default"}

    post_init_exports: list[dict[str, object]] = []

    class Normalized(Schema):
        value: str
        cached: bool = False

        def __post_init__(self) -> None:
            object.__setattr__(self, "cached", self.value == "known")
            post_init_exports.append(self.serialize(exclude_unset=True))

    normalized = Normalized.from_model(type("Row", (), {"value": "known"})())
    assert normalized.cached is True
    assert normalized.serialize(exclude_unset=True) == {"value": "known"}
    assert post_init_exports == [{"value": "known"}]


def test_selectors_are_shallow_python_names_and_aliases_are_recursive() -> None:
    card = Card.validate({"displayName": "Aki"})
    board = Board(title="notes", cards=[card])

    assert board.serialize(include={"cards"}, exclude_unset=True) == {
        "cards": [{"displayName": "Aki"}]
    }
    assert board.serialize(include={"cards"}, by_alias=False) == {
        "cards": [{"display_name": "Aki", "note": "new"}]
    }
    assert board.serialize(include=set()) == {}
    assert board.serialize(include={"title", "cards"}, exclude={"title"}) == {
        "cards": [card.dump()]
    }
    assert board.serialize(exclude={"title"}) == {"cards": [card.dump()]}


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"include": "title"}, TypeError, "not str"),
        ({"exclude": ["title", 2]}, TypeError, "members must be strings"),
        ({"include": {"displayName"}}, ValueError, "unknown include fields"),
        ({"exclude": ["missing"]}, ValueError, "unknown exclude fields"),
        ({"exclude": None}, TypeError, "must be an iterable"),
        ({"exclude_unset": 1}, TypeError, "must be bool"),
    ],
)
def test_invalid_serialization_options_fail_before_export(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        Board(title="x", cards=[]).serialize(**kwargs)  # type: ignore[arg-type]


def test_tracking_survives_copy_pickle_and_replace() -> None:
    original = Defaults(amount=0)
    assert replace(original).serialize(exclude_unset=True) == {
        "label": "default",
        "amount": 0,
        "enabled": False,
        "note": None,
        "items": [],
    }
    # dataclasses.replace passes every field to the constructor by design.
    restored = loads(dumps(original))  # noqa: S301 - round trip our own local object
    assert restored.serialize(exclude_unset=True) == {"amount": 0}
    assert copy(original).serialize(exclude_unset=True) == {"amount": 0}


def test_custom_dump_cannot_be_filtered_and_untracked_init_is_explicit() -> None:
    class Secret(Schema):
        public: str
        hidden: str

        def dump(self) -> dict[str, str]:
            return {"public": self.public}

    class Envelope(Schema):
        title: str
        secret: Secret

    secret = Secret(public="ok", hidden="private")
    assert secret.dump() == {"public": "ok"}
    envelope = Envelope(title="x", secret=secret)
    assert envelope.dump() == {"title": "x", "secret": {"public": "ok"}}
    assert json.loads(core.encode_json(envelope)) == {
        "title": "x",
        "secret": {"public": "ok"},
    }
    with pytest.raises(TypeError, match="overrides dump"):
        secret.serialize()
    with pytest.raises(TypeError, match="overrides dump"):
        envelope.serialize()
    assert envelope.serialize(include={"title"}) == {"title": "x"}
    with pytest.raises(TypeError, match="no reliable supplied-field tracking"):
        CustomInit(value="x").serialize(exclude_unset=True)


class CustomInit(Schema):
    value: str

    def __init__(self, *, value: str) -> None:
        object.__setattr__(self, "value", value)


def test_custom_init_supports_unfiltered_explicit_serialization() -> None:
    value = CustomInit(value="ok")
    assert value.serialize() == {"value": "ok"}


def test_custom_subclass_cannot_inherit_partial_presence_metadata() -> None:
    class Parent(Schema):
        inherited: int = 1

    class CustomChild(Parent):
        own: int = 2

        def __init__(self, *, inherited: int = 1, own: int = 2) -> None:
            super().__init__(inherited=inherited)
            object.__setattr__(self, "own", own)

    child = CustomChild(inherited=5, own=8)
    assert child.serialize() == {"inherited": 5, "own": 8}
    with pytest.raises(TypeError, match="no reliable supplied-field tracking"):
        child.serialize(exclude_unset=True)


def test_base_constructor_cannot_overwrite_derived_presence_metadata() -> None:
    class Parent(Schema):
        inherited: int = 1

    class Derived(Parent):
        own: int = 2

        def __post_init__(self) -> None:
            super().__init__(inherited=self.inherited)

    child = Derived(inherited=5, own=8)
    assert child.serialize(exclude_unset=True) == {"inherited": 5, "own": 8}


def test_generated_constructor_signature_and_reserved_metadata() -> None:
    signature = inspect.signature(Defaults)
    assert tuple(signature.parameters) == (
        "label",
        "amount",
        "enabled",
        "note",
        "items",
        "patch",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    with pytest.raises(RegistrationError, match="reserved by Hinoki"):

        class Bad(Schema):
            __hinoki_supplied_fields__: str

    with pytest.raises(
        RegistrationError, match=r"conflicts with the Schema\.serialize"
    ):

        class Colliding(Schema):
            # Runtime must reject this instead of inheriting the method default.
            serialize: str  # type: ignore[assignment]

    @dataclass(frozen=True, kw_only=True)
    class Mixin:
        serialize: str

    with pytest.raises(
        RegistrationError, match=r"conflicts with the Schema\.serialize"
    ):
        type("Combined", (Mixin, Schema), {})

    @dataclass(frozen=True, kw_only=True)
    class TrackingMixin:
        __hinoki_supplied_fields__: str

    with pytest.raises(RegistrationError, match="tracking metadata is reserved"):
        type("TrackingCombined", (TrackingMixin, Schema), {})
