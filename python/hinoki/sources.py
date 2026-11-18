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

XML = Annotated[S, TextSource("xml")]
"""``XML[Order]``: a ``Schema`` read from an ``application/xml`` body (see
``XMLParser``), with its text values converted to the types the ``Schema``
declares."""

Query = Annotated[S, TextSource("query")]
"""``Query[Filters]``: a ``Schema`` read from the whole query string, with its
text values converted to the types the ``Schema`` declares. A repeated key fills
a ``list`` field."""
