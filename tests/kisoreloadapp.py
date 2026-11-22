"""An application for the SIGHUP reload tests, run by ``hinoki serve`` in a
subprocess. ``RELOAD_STATE`` is a directory the test shares with it: a file named
``broken`` there makes the next worker fail to import; ``/hold`` leaves
``held-<pid>`` there and waits for ``SIGUSR1``."""

import asyncio
import os
import signal
from pathlib import Path

from hinoki import Hinoki, http
from hinoki.settings import AppSettings, MetricsSettings, ServerSettings

STATE = Path(os.environ["RELOAD_STATE"])

if (STATE / "broken").exists():
    raise RuntimeError("this version of the code does not start")


@http.get("/pid")
async def pid() -> dict[str, int]:
    return {"pid": os.getpid()}


@http.get("/hold")
async def hold() -> dict[str, int]:
    released: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGUSR1, released.set_result, None
    )
    (STATE / f"held-{os.getpid()}").touch()
    await released
    return {"pid": os.getpid()}


def create_app() -> Hinoki:
    metrics = MetricsSettings(listen=f"127.0.0.1:{os.environ['METRICS_PORT']}")
    sighup = os.environ.get("RELOAD_SIGHUP")
    server = ServerSettings(sighup=sighup)  # type: ignore[arg-type]
    app = Hinoki(settings=AppSettings(metrics=metrics, server=server))
    app.include(pid, hold)
    return app
