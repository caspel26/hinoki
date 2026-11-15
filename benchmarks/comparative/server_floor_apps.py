"""Minimal Granian applications: the floor Hinoki's own work is measured against."""

BODY = b'{"status":"ok"}'
HEADERS = [(b"content-type", b"application/json"), (b"content-length", b"15")]


async def asgi(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": HEADERS})
        await send({"type": "http.response.body", "body": BODY})
        return
    while scope["type"] == "lifespan":
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        else:
            await send({"type": "lifespan.shutdown.complete"})
            return


async def rsgi(scope, proto):  # type: ignore[no-untyped-def]
    proto.response_str(200, [("content-type", "application/json")], BODY.decode())
