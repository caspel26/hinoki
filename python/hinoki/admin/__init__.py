"""The optional admin extra: ``from hinoki.admin import Admin, ModelAdmin``.

Importing this package registers nothing and touches no database.
"""

from hinoki.admin.core import Admin, ModelAdmin

__all__ = ["Admin", "ModelAdmin"]
