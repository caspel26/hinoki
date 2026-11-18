"""Response renderers, chosen from the request's ``Accept`` header.

``@http.get("/x", renderers=[JSONRenderer(), CSVRenderer()])`` lists what an action
can answer; the default is JSON only. Negotiation (quality values, wildcards,
specificity) runs in Rust; with no acceptable renderer the answer is 406 before
the handler runs. A renderer receives the JSON-compatible form of the handler's
result and returns bytes. Error bodies are always JSON.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from hinoki import core
from hinoki.http.request import Request

MAX_INDENT = 16
MAX_XML_DEPTH = 200
_XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_XML_ILLEGAL = re.compile("[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
# Applied in order, "&" first so no reference is escaped twice.
_XML_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("\t", "&#9;"),
    ("\n", "&#10;"),
    ("\r", "&#13;"),
)
_XML_SPECIAL = re.compile('[&<>"\t\n\r]')
_XML_KEY_ERROR = "keys must be str, int, bool or None"


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


def xml_text(value: str) -> str:
    """Escaped text, safe in content and in attributes. Tab and line breaks are
    written as references so a parser's whitespace normalization keeps them;
    characters XML 1.0 cannot carry become U+FFFD."""
    clean = _XML_ILLEGAL.sub("\ufffd", value)
    if _XML_SPECIAL.search(clean) is None:
        return clean
    for special, reference in _XML_ESCAPES:
        clean = clean.replace(special, reference)
    return clean


def xml_key(key: object) -> str:
    """The text of a mapping key, spelled as the JSON encoder spells it: a
    ``str`` as is, ``None`` as ``null``, a ``bool`` as ``true``/``false`` and an
    ``int`` in decimal. Any other key is refused, as JSON refuses it."""
    if isinstance(key, str):
        return str.__str__(key)
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, int):
        return int.__repr__(key)
    raise TypeError(_XML_KEY_ERROR)


def xml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return xml_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("INF" if value > 0 else "-INF")
    if isinstance(value, int | float):
        try:
            return str(value)
        except ValueError:
            raise ValueError("the integer is too large to write as XML") from None
    raise TypeError(f"XMLRenderer cannot render a {type(value).__name__}")


class XMLRenderer(Renderer):
    """The JSON-compatible result as XML (UTF-8, with an XML declaration).

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

    def __init__(self, root: str = "response", item: str = "item") -> None:
        if not (valid_xml_name(root) and valid_xml_name(item)):
            raise ValueError("root and item must be valid XML element names")
        self.root = root
        self.item = item

    def render(self, value: object, request: Request) -> bytes:  # noqa: ARG002
        parts = ['<?xml version="1.0" encoding="UTF-8"?>']
        self._element(parts, self.root, "", value, 0)
        return "".join(parts).encode()

    def _element(
        self, parts: list[str], name: str, attrs: str, value: object, depth: int
    ) -> None:
        if depth > MAX_XML_DEPTH:
            raise ValueError("the value is nested too deeply for XML")
        if value is None:
            parts.append(f"<{name}{attrs}/>")
            return
        parts.append(f"<{name}{attrs}>")
        if isinstance(value, dict):
            for key, child in value.items():
                tag, extra = self._tag(xml_key(key))
                self._element(parts, tag, extra, child, depth + 1)
        elif isinstance(value, list | tuple):
            for child in value:
                self._element(parts, self.item, "", child, depth + 1)
        else:
            parts.append(xml_scalar(value))
        parts.append(f"</{name}>")

    def _tag(self, key: str) -> tuple[str, str]:
        if valid_xml_name(key):
            return key, ""
        return self.item, f' key="{xml_text(key)}"'


def valid_xml_name(name: str) -> bool:
    return bool(_XML_NAME.match(name)) and not name.lower().startswith("xml")


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
