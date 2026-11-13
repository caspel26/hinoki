"""Response renderers, chosen from the request's ``Accept`` header.

``@get("/x", renderers=[JSONRenderer(), CSVRenderer()])`` lists what an action
can answer; the default is JSON only. Negotiation (quality values, wildcards,
specificity) runs in Rust; with no acceptable renderer the answer is 406 before
the handler runs. A renderer receives the JSON-compatible form of the handler's
result and returns bytes. Error bodies are always JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from hinoki import native
from hinoki.request import Request

MAX_INDENT = 16


class Renderer:
    """Base class of response renderers."""

    media_type: ClassVar[str] = ""
    # True when ``render`` can take schema instances, UUIDs and datetimes as
    # they are; otherwise it receives the JSON-compatible form.
    consumes_objects: ClassVar[bool] = False

    def render(self, value: object, request: Request) -> bytes:
        raise NotImplementedError


class JSONRenderer(Renderer):
    """JSON, written by the Rust encoder. Customise without leaving Rust:

    * options: ``JSONRenderer(indent=2, sort_keys=True, default=fn)`` where
      ``default`` turns a value that is not JSON (a ``Decimal``, a ``Path``) into
      one that is;
    * a subclass overriding ``prepare(value, request)`` to reshape the result
      (envelopes, key renaming) before it is encoded, and/or ``media_type`` (for
      ``application/vnd.api+json``).
    """

    media_type = "application/json"
    consumes_objects = True

    def __init__(
        self,
        indent: int | None = None,
        sort_keys: bool = False,
        default: Callable[[Any], Any] | None = None,
    ) -> None:
        if indent is not None and not 0 <= indent <= MAX_INDENT:
            raise ValueError(f"indent must be 0 to {MAX_INDENT} or None")
        self.indent = indent
        self.sort_keys = sort_keys
        self.default = default

    def prepare(self, value: object, request: Request) -> object:  # noqa: ARG002
        """Hook: the value to encode (default: unchanged)."""
        return value

    def render(self, value: object, request: Request) -> bytes:
        return native.encode_json(
            self.prepare(value, request), self.indent, self.sort_keys, self.default
        )


class HTMLRenderer(Renderer):
    """A handler that returns ``str`` answers HTML (UTF-8). The string is sent
    as is: escape untrusted data before putting it in."""

    media_type = "text/html; charset=utf-8"

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        if not isinstance(value, str):
            raise TypeError("HTMLRenderer needs the handler to return a str")
        return value.encode()


class TextRenderer(Renderer):
    """A handler that returns ``str`` answers plain text (UTF-8)."""

    media_type = "text/plain; charset=utf-8"

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        if not isinstance(value, str):
            raise TypeError("TextRenderer needs the handler to return a str")
        return value.encode()


DEFAULT_RENDERERS: tuple[Renderer, ...] = (JSONRenderer(),)


def check_renderers(renderers: Sequence[Renderer]) -> tuple[Renderer, ...]:
    """Fail at declaration: at least one renderer, distinct media types."""
    found = tuple(renderers)
    types = [r.media_type for r in found]
    if not found or "" in types or len(set(types)) != len(types):
        raise ValueError(
            "renderers must be a non-empty list of renderers with distinct media types"
        )
    return found


def negotiate(accept: str, renderers: Sequence[Renderer]) -> Renderer | None:
    """The renderer that best fits ``accept`` (``None`` means 406)."""
    index = native.negotiate_media(accept, [r.media_type for r in renderers])
    return None if index is None else renderers[index]
