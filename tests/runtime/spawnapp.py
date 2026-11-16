"""A handler that starts a long-lived child, as application code may."""

import subprocess

from hinoki import Hinoki, http


@http.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@http.get("/spawn")
async def spawn() -> dict[str, int]:
    # ``close_fds=False``: the child keeps every descriptor that is inheritable.
    child = subprocess.Popen(["sleep", "25"], close_fds=False)  # noqa: S607
    return {"pid": child.pid}


app = Hinoki()
app.include(health, spawn)
