"""Built-in admin interface: ``from hinoki.admin import Admin, ModelAdmin``.

Shipped in ``hinoki-api`` with explicit mounting and migrations. Importing this
package registers nothing and touches no database.
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
