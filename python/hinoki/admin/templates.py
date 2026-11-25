"""A small template engine for admin components, and the theme's assets.

A component is a ``.html`` file with ``{{ name }}`` placeholders (HTML-escaped)
and ``{{{ name }}}`` placeholders (raw, only for ``Raw`` values, i.e. HTML that
a component already produced). There is no logic in templates. A placeholder
without a value is an error, never silently empty. Resolution order: the
application's templates directory, then the built-in set; only the built-in
component names are looked up, so a name cannot walk the filesystem.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from hinoki import core
from hinoki.admin.themes import Theme, resolve

UI = Path(__file__).parent / "ui"


@functools.cache
def component_names() -> tuple[str, ...]:
    """The built-in component names, read from disk on first use."""
    return tuple(sorted(p.stem for p in (UI / "components").glob("*.html")))


class TemplateError(Exception):
    """A template cannot be rendered with the values given."""


class Raw(str):
    """HTML produced by a component; the only kind of value ``{{{ }}}`` accepts."""

    __slots__ = ()


def escape(value: object) -> Raw:
    """HTML-escape any value (attributes included) into a ``Raw`` string."""
    return Raw(core.html_escape(str(value)))


def placeholders(source: str) -> set[tuple[str, bool]]:
    """Every ``(name, is_raw)`` placeholder in a component's source."""
    return set(core.template_placeholders(source))


class Templates:
    """The components, stylesheet, script and icons of one admin, loaded once."""

    def __init__(
        self, directory: str | Path | None = None, theme: str | Theme = "studio"
    ) -> None:
        self.directory = Path(directory) if directory else None
        self.theme = resolve(theme)
        self.check_overrides()
        self.sources = {name: self.read(name) for name in component_names()}
        self._compiled = {
            name: core.AdminTemplate(src) for name, src in self.sources.items()
        }
        self.sprite = Raw((UI / "icons.svg").read_text(encoding="utf-8"))

    def check_overrides(self) -> None:
        """Refuse a bad override now, not on the first request: a file that names
        no component, a placeholder the component is never given, or a form that
        lost its ``csrf`` token (which would make every post fail)."""
        if self.directory is None:
            return
        for path in sorted((self.directory / "components").glob("*.html")):
            if path.stem not in component_names():
                raise TemplateError(
                    f"{path.name}: no admin component has this name; "
                    f"the components are {', '.join(component_names())}"
                )
            builtin = placeholders((UI / "components" / path.name).read_text("utf-8"))
            custom = placeholders(path.read_text(encoding="utf-8"))
            for name, is_raw in sorted(custom - builtin):
                marks = "{{{ %s }}}" if is_raw else "{{ %s }}"
                raise TemplateError(
                    f"{path.stem}: {marks % name} is never given a value there"
                )
            if any(n == "csrf" for n, _ in builtin) and not any(
                n == "csrf" for n, _ in custom
            ):
                raise TemplateError(f"{path.stem}: the override drops the csrf token")

    def read(self, name: str) -> str:
        if self.directory is not None:
            custom = self.directory / "components" / f"{name}.html"
            if custom.is_file():
                return custom.read_text(encoding="utf-8")
        return (UI / "components" / f"{name}.html").read_text(encoding="utf-8")

    def stylesheet(self) -> bytes:
        """Structure, then the theme's tokens, then the application's ``admin.css``
        (if any), so an application can override anything last."""
        parts = [(UI / "base.css").read_text(encoding="utf-8"), self.theme.tokens_css()]
        if self.directory is not None and (self.directory / "admin.css").is_file():
            parts.append((self.directory / "admin.css").read_text(encoding="utf-8"))
        return "\n".join(parts).encode()

    def script(self) -> bytes:
        return (UI / "admin.js").read_bytes()

    def render(self, component: str, /, **values: Any) -> Raw:
        compiled = self._compiled.get(component)
        if compiled is None:
            raise TemplateError(f"unknown component {component!r}")
        try:
            return Raw(compiled.render(values, Raw))
        except KeyError as exc:
            key = exc.args[0]
            raise TemplateError(f"{component}: no value for {key!r}") from None
        except TypeError as exc:
            key = exc.args[0]
            raise TemplateError(
                f"{component}: {key!r} is raw but not a Raw value"
            ) from None
