"""An application that takes a while to import (``SLOW_IMPORT`` seconds)."""

import os
import time

from hinoki import Hinoki, http

time.sleep(float(os.environ.get("SLOW_IMPORT", "0")))


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app = Hinoki()
app.include(health)
