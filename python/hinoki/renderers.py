"""Response renderers, chosen from the request's ``Accept`` header.

``@http.get("/x", renderers=[JSONRenderer(), CSVRenderer()])`` lists what an action
can answer; the default is JSON only. Negotiation (quality values, wildcards,
specificity) runs in Rust; with no acceptable renderer the answer is 406 before
the handler runs. A renderer receives the JSON-compatible form of the handler's
result and returns bytes. Error bodies are always JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from hinoki import core
from hinoki.http.request import Request

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
        return core.encode_json(
            self.prepare(value, request), self.indent, self.sort_keys, self.default
        )


def is_default_json_renderer(renderer: Renderer) -> bool:
    """Whether this exact renderer has no custom behavior or JSON options."""
    return (
        type(renderer) is JSONRenderer
        and renderer.media_type == "application/json"
        and renderer.indent is None
        and not renderer.sort_keys
        and renderer.default is None
        and set(vars(renderer)) == {"indent", "sort_keys", "default"}
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


class XMLRenderer(Renderer):
    """The result as XML (UTF-8, with an XML declaration), written by the Rust
    encoder, which accepts what the JSON encoder accepts.

    A mapping becomes child elements named by its keys, a list becomes repeated
    ``item`` elements, ``None`` an empty element, and ``true``/``false`` are
    written in lower case. Keys are spelled as JSON spells them (``None`` as
    ``null``, booleans ``true``/``false``, integers in decimal) and any other
    key is refused, as JSON refuses it. A key that is not a valid XML name (it
    has a space, starts with a digit, begins with ``xml``) is written as
    ``<item key="...">`` instead of failing. Characters XML 1.0 cannot carry
    are replaced by U+FFFD. ``XMLRenderer(root="orders", item="order")`` renames
    the root element and the list item.
    """

    media_type = "application/xml; charset=utf-8"
    consumes_objects = True

    def __init__(self, root: str = "response", item: str = "item") -> None:
        if not (core.xml_name_ok(root) and core.xml_name_ok(item)):
            raise ValueError("root and item must be valid XML element names")
        self.root = root
        self.item = item

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        return core.encode_xml(value, self.root, self.item)


_PLAIN_RENDERER_STATE: dict[type[Renderer], set[str]] = {
    JSONRenderer: {"indent", "sort_keys", "default"},
    HTMLRenderer: set(),
    TextRenderer: set(),
    XMLRenderer: {"root", "item"},
}


def is_native_renderer(renderer: Renderer) -> bool:
    """Whether this is exactly a built-in renderer with only its documented
    options, so the native handler lane may call it without a ``Request``."""
    state = _PLAIN_RENDERER_STATE.get(type(renderer))
    return state is not None and set(vars(renderer)) == state


DEFAULT_RENDERERS: tuple[Renderer, ...] = (JSONRenderer(),)


def check_renderers(renderers: Sequence[Renderer]) -> tuple[Renderer, ...]:
    """Fail at declaration: at least one renderer, distinct media types."""
    found = (*renderers,)  # always a new tuple: identity marks "not chosen"
    types = [r.media_type for r in found]
    if not found or "" in types or len(set(types)) != len(types):
        raise ValueError(
            "renderers must be a non-empty list of renderers with distinct media types"
        )
    return found


def negotiate(accept: str, renderers: Sequence[Renderer]) -> Renderer | None:
    """The renderer that best fits ``accept`` (``None`` means 406)."""
    index = core.negotiate_media(accept, [r.media_type for r in renderers])
    return None if index is None else renderers[index]
