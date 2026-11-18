"""Markers that say where a ``Schema`` parameter reads its values from.

A plain ``Schema`` parameter is a strict JSON body. ``Form[Signup]`` reads the
same ``Schema`` from a text source, a form, and converts text to the declared
scalar types first (``"2"`` for an ``int``); the ``Schema`` itself stays strict.
"""

import dataclasses
from typing import Annotated, TypeVar

S = TypeVar("S")


@dataclasses.dataclass(frozen=True, slots=True)
class TextSource:
    """The kind of text source an annotated ``Schema`` parameter reads."""

    kind: str


Form = Annotated[S, TextSource("form")]
"""``Form[Signup]``: a ``Schema`` read from a form body, with its text values
converted to the types the ``Schema`` declares."""
