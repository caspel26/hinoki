"""Request body parsers, chosen by ``Content-Type``.

``@post("/x", parsers=[JSONParser(), FormParser(), MultiPartParser()])`` lists
what an action accepts; the default is JSON only. The built-in parsers decode in
Rust. A custom parser is a ``Parser`` subclass with a ``media_type`` and a
``parse`` method (Python, so the slow path): it returns ``Parsed`` and may raise
``InvalidBody``. Limits are explicit and a violation is an error, never a
truncation.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import ClassVar

from hinoki import native
from hinoki.errors import InvalidBody

DEFAULT_FIELDS = 1000
DEFAULT_PARTS = 100
DEFAULT_PART_BYTES = 1024 * 1024


class MultiDict[V](Mapping[str, V]):
    """Read-only mapping with several values per name. ``d["a"]`` and ``get``
    return the last value, as in Django; ``getlist`` returns all of them."""

    __slots__ = ("lists",)

    def __init__(self, lists: Mapping[str, list[V]]) -> None:
        self.lists = dict(lists)

    def __getitem__(self, name: str) -> V:
        return self.lists[name][-1]

    def __iter__(self) -> Iterator[str]:
        return iter(self.lists)

    def __len__(self) -> int:
        return len(self.lists)

    def getlist(self, name: str) -> list[V]:
        return list(self.lists.get(name, []))


class QueryDict(MultiDict[str]):
    """Query parameters or form fields."""

    __slots__ = ()


@dataclasses.dataclass(frozen=True, slots=True)
class UploadedFile:
    """A multipart file. ``filename`` is exactly what the client sent: never
    use it as a path."""

    name: str
    filename: str
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclasses.dataclass(frozen=True, slots=True)
class Parsed:
    """What a parser produced: ``data`` for ``request.data`` plus form fields
    and files for ``request.POST`` / ``request.FILES``."""

    data: object
    fields: Mapping[str, list[str]] = dataclasses.field(default_factory=dict)
    files: Mapping[str, list[UploadedFile]] = dataclasses.field(default_factory=dict)


class Parser:
    """Base class of body parsers."""

    media_type: ClassVar[str] = ""
    # True when the parser fills ``Parsed.fields`` / ``files`` (request.POST/FILES).
    forms: ClassVar[bool] = False

    def parse(self, raw: bytes, content_type: str) -> Parsed:
        raise NotImplementedError


class JSONParser(Parser):
    """JSON, decoded by the Rust parser (strict: no NaN, no lone surrogates, depth
    limit). Customise by overriding ``prepare(data)`` to reshape the decoded value
    (for example rename keys) or ``media_type`` (``application/vnd.api+json``)."""

    media_type = "application/json"

    def prepare(self, data: object) -> object:
        """Hook: the value the action sees (default: as decoded)."""
        return data

    def parse(self, raw: bytes, content_type: str) -> Parsed:  # noqa: ARG002
        try:
            data = native.decode_json(raw)
        except ValueError:
            raise InvalidBody("invalid_json", "the body is not valid JSON") from None
        return Parsed(self.prepare(data))  # a bug in the hook is not a bad body


class TextParser(Parser):
    """``text/plain`` bodies as a ``str`` (UTF-8, strict)."""

    media_type = "text/plain"

    def parse(self, raw: bytes, content_type: str) -> Parsed:  # noqa: ARG002
        try:
            return Parsed(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise InvalidBody("invalid_text", "the body is not valid UTF-8") from None


def group(pairs: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for name, value in pairs:
        fields.setdefault(name, []).append(value)
    return fields


class FormParser(Parser):
    """``application/x-www-form-urlencoded``. ``data`` holds the last value of
    each field as text."""

    media_type = "application/x-www-form-urlencoded"
    forms = True

    def __init__(self, max_fields: int = DEFAULT_FIELDS) -> None:
        if max_fields < 1:
            raise ValueError("max_fields must be positive")
        self.max_fields = max_fields

    def parse(self, raw: bytes, content_type: str) -> Parsed:  # noqa: ARG002
        try:
            fields = native.parse_query_string(raw, self.max_fields)
        except ValueError:
            raise InvalidBody("invalid_form", "malformed form body") from None
        return Parsed({name: values[-1] for name, values in fields.items()}, fields)


BOUNDARY = re.compile(r';\s*boundary=(?:"([^"]*)"|([^;\s]*))', re.IGNORECASE)


def boundary_of(content_type: str) -> str:
    """The ``boundary`` parameter, quoted or not (a quoted one may hold ``;``)."""
    found = BOUNDARY.search(content_type)
    return (found.group(1) or found.group(2) or "") if found else ""


class MultiPartParser(Parser):
    """``multipart/form-data`` with at most ``max_parts`` parts of at most
    ``max_part_bytes`` bytes each (the whole body is capped separately)."""

    media_type = "multipart/form-data"
    forms = True

    def __init__(
        self, max_parts: int = DEFAULT_PARTS, max_part_bytes: int = DEFAULT_PART_BYTES
    ) -> None:
        if max_parts < 1 or max_part_bytes < 1:
            raise ValueError("multipart limits must be positive")
        self.max_parts = max_parts
        self.max_part_bytes = max_part_bytes

    def parse(self, raw: bytes, content_type: str) -> Parsed:
        try:
            pairs, found = native.parse_multipart_body(
                raw, boundary_of(content_type), self.max_parts, self.max_part_bytes
            )
        except ValueError as exc:
            raise InvalidBody("invalid_multipart", str(exc)) from None
        files: dict[str, list[UploadedFile]] = {}
        for name, filename, kind, content in found:
            files.setdefault(name, []).append(
                UploadedFile(name, filename, kind, content)
            )
        fields = group(pairs)
        data = {name: values[-1] for name, values in fields.items()}
        return Parsed(data, fields, files)


DEFAULT_PARSERS: tuple[Parser, ...] = (JSONParser(),)


def check_parsers(parsers: Sequence[Parser]) -> tuple[Parser, ...]:
    """Fail at declaration: at least one parser, each with a distinct media type."""
    found = (*parsers,)  # always a new tuple: identity marks "not chosen"
    types = [p.media_type for p in found]
    if not found or "" in types or len(set(types)) != len(types):
        raise ValueError(
            "parsers must be a non-empty list of parsers with distinct media types"
        )
    return found


def choose(parsers: Sequence[Parser], content_type: str) -> Parser:
    """The parser for ``content_type`` (a bare, lower-case media type)."""
    for parser in parsers:
        if parser.media_type == content_type:
            return parser
    expected = ", ".join(p.media_type for p in parsers)
    raise InvalidBody("unsupported_media_type", f"expected {expected}")
