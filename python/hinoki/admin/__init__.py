"""The optional admin extra: ``from hinoki.admin import Admin, ModelAdmin``.

Importing this package registers nothing and touches no database.
"""

from hinoki.admin.core import Admin
from hinoki.admin.modeladmin import BulkAction, ModelAdmin, action
from hinoki.admin.parts import (
    Avatar,
    Badge,
    Event,
    Extra,
    Fieldset,
    Link,
    Refusal,
    Stat,
    Tab,
    Tone,
)
from hinoki.admin.themes import THEMES, Theme

__all__ = [
    "THEMES",
    "Admin",
    "Avatar",
    "Badge",
    "BulkAction",
    "Event",
    "Extra",
    "Fieldset",
    "Link",
    "ModelAdmin",
    "Refusal",
    "Stat",
    "Tab",
    "Theme",
    "Tone",
    "action",
]
