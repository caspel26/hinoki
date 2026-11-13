"""Moved to ``hinoki.orm.relations``; importing this module returns that one, so old
imports keep working until the first release."""

import sys

from hinoki.orm import relations

sys.modules[__name__] = relations
