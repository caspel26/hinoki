"""A small template engine for admin components.

A component is a ``.html`` file with ``{{ name }}`` placeholders (HTML-escaped)
and ``{{{ name }}}`` placeholders (raw, only for ``Raw`` values, i.e. HTML that
a component already produced). There is no logic in templates. A placeholder
without a value is an error, never silently empty. Resolution order: the
application's templates directory, then the built-in Classic theme; only the
known component names are looked up, so a name cannot walk the filesystem.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

BUILTIN = Path(__file__).parent / "theme"
COMPONENTS = ("layout", "table", "form", "field", "notice", "confirm", "index")
PLACEHOLDER = re.compile(r"\{\{\{\s*(\w+)\s*\}\}\}|\{\{\s*(\w+)\s*\}\}")


class TemplateError(Exception):
    """A template cannot be rendered with the values given."""


class Raw(str):
    """HTML produced by a component; the only kind of value ``{{{ }}}`` accepts."""

    __slots__ = ()


def escape(value: object) -> Raw:
    """HTML-escape any value (attributes included) into a ``Raw`` string."""
    return Raw(html.escape(str(value), quote=True))


class Templates:
    """The components of one admin, loaded once."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else None
        self.sources = {name: self.read(name) for name in COMPONENTS}

    def read(self, name: str) -> str:
        if self.directory is not None:
            custom = self.directory / f"{name}.html"
            if custom.is_file():
                return custom.read_text(encoding="utf-8")
        return (BUILTIN / f"{name}.html").read_text(encoding="utf-8")

    def stylesheet(self) -> bytes:
        """``admin.css`` from the application directory, else the built-in one."""
        if self.directory is not None and (self.directory / "admin.css").is_file():
            return (self.directory / "admin.css").read_bytes()
        return (BUILTIN / "admin.css").read_bytes()

    def render(self, component: str, /, **values: Any) -> Raw:
        source = self.sources.get(component)
        if source is None:
            raise TemplateError(f"unknown component {component!r}")
        return Raw(PLACEHOLDER.sub(Filler(component, values), source))


class Filler:
    """Replacement function for one render: looks values up and escapes them."""

    def __init__(self, component: str, values: dict[str, Any]) -> None:
        self.component = component
        self.values = values

    def __call__(self, found: re.Match[str]) -> str:
        raw_name, plain_name = found.groups()
        key = raw_name or plain_name
        if key not in self.values:
            raise TemplateError(f"{self.component}: no value for {key!r}")
        value = self.values[key]
        if raw_name:
            if not isinstance(value, Raw):
                raise TemplateError(
                    f"{self.component}: {key!r} is raw but not a Raw value"
                )
            return value
        return html.escape(str(value), quote=True)
