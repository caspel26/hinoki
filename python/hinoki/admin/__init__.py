"""The optional admin extra: ``from hinoki.admin import Admin, ModelAdmin``.

Importing this package registers nothing and touches no database.
"""

from hinoki.admin.core import Admin
from hinoki.admin.modeladmin import ModelAdmin
from hinoki.admin.parts import Avatar, Badge, Event, Fieldset, Link, Stat, Tab
from hinoki.admin.themes import THEMES, Theme

__all__ = [
    "THEMES",
    "Admin",
    "Avatar",
    "Badge",
    "Event",
    "Fieldset",
    "Link",
    "ModelAdmin",
    "Stat",
    "Tab",
    "Theme",
]
