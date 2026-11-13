"""Hand-written ASGI app used to characterise Granian's lifespan behaviour.

It is not part of Hinoki: it records what the *server* does so the future
adapter can rely on it. Events are appended to the file named by EXP_LOG.
"""

import asyncio
import os
import time

LOG = os.environ["EXP_LOG"]
MODE = os.environ.get("EXP_MODE", "ok")


def log(message: str) -> None:
    with open(LOG, "a") as fh:
        fh.write(f"{time.time():.3f} {os.getpid()} {message}\n")


async def app(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                if MODE == "fail":
                    log("startup-failed")
                    await send({"type": "lifespan.startup.failed", "message": "boom"})
                    return
                log("startup")
                await send({"type": "lifespan.startup.complete"})
            else:
                log("shutdown")
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["path"] == "/slow":
        log("slow-start")
        await asyncio.sleep(1.5)
        log("slow-end")
    body = str(os.getpid()).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})
