"""Moved to ``hinoki.orm.migrations``; importing this module returns that one, so old
imports keep working until the first release."""

import sys

from hinoki.orm import migrations

sys.modules[__name__] = migrations
