"""Response renderers, chosen from the request's ``Accept`` header.

``@get("/x", renderers=[JSONRenderer(), CSVRenderer()])`` lists what an action
can answer; the default is JSON only. Negotiation (quality values, wildcards,
specificity) runs in Rust; with no acceptable renderer the answer is 406 before
the handler runs. A renderer receives the JSON-compatible form of the handler's
result and returns bytes. Error bodies are always JSON.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from hinoki import native
from hinoki.request import Request


class Renderer:
    """Base class of response renderers."""

    media_type: ClassVar[str] = ""

    def render(self, value: object, request: Request) -> bytes:
        raise NotImplementedError


class JSONRenderer(Renderer):
    media_type = "application/json"

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        return native.encode_json(value)


class HTMLRenderer(Renderer):
    """A handler that returns ``str`` answers HTML (UTF-8). The string is sent
    as is: escape untrusted data before putting it in."""

    media_type = "text/html; charset=utf-8"

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        if not isinstance(value, str):
            raise TypeError("HTMLRenderer needs the handler to return a str")
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
