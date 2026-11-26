"""A ``TypedDict`` return type with a key that does not resolve is refused at
bind time: left whole it would be written with every key it holds."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import pytest

from hinoki import Hinoki, http
from hinoki.errors import RegistrationError

if TYPE_CHECKING:
    from nowhere import Later  # type: ignore[import-not-found]


class Note(TypedDict):
    id: int
    note: Later | None


class Tree(TypedDict):
    id: int
    children: list[Tree]


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
