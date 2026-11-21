"""JSON as ``str`` for the command line, logs, files and generated text, on the
native codec (:func:`hinoki.core.encode_json` and :func:`hinoki.core.decode_json`).

Unlike :mod:`json`, ``NaN`` and infinities are refused in both directions and
every failure to decode is a :class:`ValueError`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hinoki import core


def dumps(
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
    ensure_ascii: bool = True,
) -> str:
    """Compact JSON text; ASCII-only by default, so it is safe for any stream."""
    return core.encode_json(value, indent, sort_keys, default, ensure_ascii).decode()


def loads(raw: str | bytes | bytearray) -> Any:
    """Parse a JSON document. Raises :class:`ValueError` when it is not one."""
    if isinstance(raw, str):
        raw = raw.encode()
    return core.decode_json(bytes(raw))


def toml_value(value: str | list[str]) -> str:
    """A TOML basic string, or an array of them, for text that came from the user.

    JSON escapes control characters except DEL, which TOML forbids unescaped,
    and writes characters above the BMP as surrogate pairs, which TOML rejects;
    so the text stays UTF-8 and DEL is escaped by hand."""
    return dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
