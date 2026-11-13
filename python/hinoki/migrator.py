"""Moved to ``hinoki.orm.migrator``; importing this module returns that one, so old
imports keep working until the first release."""

import sys

from hinoki.orm import migrator

sys.modules[__name__] = migrator
