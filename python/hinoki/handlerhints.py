"""A handler's signature and type hints, the same on every supported Python.

Since Python 3.14 annotations are evaluated when they are first read, in the
scope of the class body. A method that carries the name of a builtin it also
uses in its own annotations then resolves that name to itself::

    class Posts(API):
        async def list(self) -> list[int]: ...   # `list` is now this method

Up to 3.13 the annotation was evaluated before the method was bound, so it
meant the builtin. When the normal resolution fails, the hints are resolved
again from their source text in the module's scope, which restores that
meaning.
"""

from __future__ import annotations

import inspect
import sys
import typing
from collections.abc import Callable
from typing import Any

if sys.version_info >= (3, 14):
    import annotationlib


def signature_of(handler: Callable[..., object]) -> inspect.Signature:
    """The signature without evaluating annotations: binding reads names,
    kinds and defaults from it and takes the types from ``hints_of``."""
    if sys.version_info >= (3, 14):
        return inspect.signature(handler, annotation_format=annotationlib.Format.STRING)
    return inspect.signature(handler)


def hints_of(
    handler: Callable[..., object], include_extras: bool = False
) -> dict[str, Any]:
    """``typing.get_type_hints(handler)``; raises ``NameError`` or
    ``TypeError`` when an annotation cannot be resolved."""
    try:
        return typing.get_type_hints(handler, include_extras=include_extras)
    except (NameError, TypeError):
        if sys.version_info >= (3, 14):
            written = annotationlib.get_annotations(
                handler, format=annotationlib.Format.STRING
            )
            return module_scope_hints(handler, written, include_extras)
        raise


def module_scope_hints(
    handler: Callable[..., object],
    written: dict[str, str],
    include_extras: bool = False,
) -> dict[str, Any]:
    """Hints from annotation source text, resolved in the module's scope."""
    carrier = Carrier()
    carrier.__annotations__ = dict(written)
    scope = getattr(inspect.unwrap(handler), "__globals__", {})
    return typing.get_type_hints(carrier, globalns=scope, include_extras=include_extras)


class Carrier:
    """Holds annotation source text for ``typing.get_type_hints``."""

    __annotations__: dict[str, Any]
