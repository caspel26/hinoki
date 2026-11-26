"""Bind-time checks for TypedDict annotations that cannot be narrowed."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import pytest

from hinoki import Hinoki, Schema, http
from hinoki.errors import RegistrationError

if TYPE_CHECKING:
    from nowhere import Later  # type: ignore[import-not-found]


class Note(TypedDict):
    id: int
    note: Later | None


class Tree(TypedDict):
    id: int
    children: list[Tree]


class Item(Schema):
    id: int


class Branch(TypedDict):
    item: Item
    children: list[Branch]


class Parent(TypedDict):
    child: Child | None


class Child(TypedDict):
    item: Item
    parent: Parent | None


class AliasedBranch(TypedDict):
    item: Item
    children: list[BranchAlias]


type BranchAlias = AliasedBranch


class Envelope(TypedDict):
    item: Item
    plain_tree: Tree


def action(annotation: object, **options: object) -> Hinoki:
    async def serve() -> None: ...

    serve.__annotations__ = {"return": annotation}
    return Hinoki(include=[http.get("/x", **options)(serve)])  # type: ignore[arg-type]


@pytest.mark.parametrize("annotation", [Note, list[Note], dict[str, Note], Note | None])
def test_a_typeddict_with_an_unresolved_key_stops_startup(annotation: object) -> None:
    with pytest.raises(RegistrationError) as caught:
        action(annotation, validate_response=False)
    message = str(caught.value)
    assert "serve" in message
    assert "Note" in message
    assert "'note'" in message
    assert "Later" in message


def test_a_recursive_typeddict_still_binds() -> None:
    action(Tree, validate_response=False)


def test_a_schema_can_sit_beside_a_plain_recursive_typeddict() -> None:
    action(Envelope, validate_response=False)


@pytest.mark.parametrize(
    "annotation", [Branch, list[Branch], Parent, Child, BranchAlias]
)
def test_a_recursive_typeddict_holding_a_schema_stops_startup(
    annotation: object,
) -> None:
    with pytest.raises(
        RegistrationError, match=r"serve: .*recursive TypedDict .*Schema"
    ):
        action(annotation, validate_response=False)


def test_the_response_checker_rejects_typeddict_returns() -> None:
    with pytest.raises(RegistrationError, match=r"serve: .*cannot be checked"):
        action(Branch, validate_response=True)
