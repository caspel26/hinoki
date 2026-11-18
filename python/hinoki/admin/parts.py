"""Small value types applications use to configure the admin."""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Mapping
from typing import Any


class Tone(enum.StrEnum):
    """The color role of a badge, stat or event."""

    NEUTRAL = "neutral"
    ACCENT = "accent"
    OK = "ok"
    WARN = "warn"
    DANGER = "danger"
    INFO = "info"


TONES = tuple(tone.value for tone in Tone)


@dataclasses.dataclass(frozen=True, slots=True)
class Badge:
    """A status pill. ``icon`` is a Lucide icon name; ``tone`` one of ``TONES``."""

    label: str
    tone: Tone | str = Tone.NEUTRAL
    icon: str = "circle-check"


@dataclasses.dataclass(frozen=True, slots=True)
class Avatar:
    """A person: initials in a circle plus the name."""

    name: str


@dataclasses.dataclass(frozen=True, slots=True)
class Stat:
    """A dashboard card. ``series`` (optional numbers, oldest first) draws a
    sparkline; without data there is no chart."""

    label: str
    value: int | str
    icon: str = "file-text"
    tone: Tone | str = Tone.ACCENT
    series: tuple[float, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class Event:
    """One line of the recent-activity panel."""

    text: str
    when: str
    icon: str = "pencil"
    tone: Tone | str = Tone.ACCENT


@dataclasses.dataclass(frozen=True, slots=True)
class Tab:
    """A saved filter shown as a tab: ``Tab("Drafts", {"published": False})``."""

    label: str
    filters: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class Fieldset:
    """A titled group of form fields; ``collapsed`` renders it closed."""

    title: str
    fields: tuple[str, ...]
    collapsed: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class Link:
    """An extra sidebar link (settings, activity log, ...) supplied by the app."""

    label: str
    href: str
    icon: str = "settings"


# What an activity provider and a role function look like.
ActivityProvider = Callable[[Any], Any]
