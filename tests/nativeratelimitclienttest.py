"""The rate limit sees the same client on the native and the Python lane,
including the client a trusted proxy forwards."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from hinoki import Hinoki, RateLimitContext, Request, http
from hinoki.runtime.kiso import KisoWorker
from hinoki.settings import AppSettings, ServerSettings
from tests.kisoservertest import fetch, record_lanes


@dataclass
class Recorder:
    """A rate-limit policy that admits everything and keeps what it was shown."""

    clients: list[str | None] = field(default_factory=list)

    async def check(self, context: RateLimitContext) -> None:
        self.clients.append(context.client)


NATIVE = Recorder()
PYTHON = Recorder()


@pytest.fixture(autouse=True)
def fresh_recorders() -> None:
    NATIVE.clients.clear()
    PYTHON.clients.clear()


@http.get("/where", rate_limit=NATIVE)
async def where() -> dict[str, bool]:
    return {"ok": True}


@http.get("/where-python", rate_limit=PYTHON)
async def where_python(request: Request) -> dict[str, bool]:
    return {"ok": True}


# The test client connects from 127.0.0.1.
TRUSTED = ("127.0.0.1",)
CHAIN = ("127.0.0.1", "10.0.0.0/8")
UNTRUSTED = ("10.0.0.0/8",)
CLIENT = "203.0.113.9"
PEER = "127.0.0.1"


@pytest.mark.parametrize(
    ("proxies", "extra", "expected"),
    [
        (TRUSTED, f"x-forwarded-for: {CLIENT}\r\n", CLIENT),
        (UNTRUSTED, f"x-forwarded-for: {CLIENT}\r\n", PEER),
        ((), f"x-forwarded-for: {CLIENT}\r\n", PEER),
        (TRUSTED, f"x-forwarded-for: {CLIENT}, 127.0.0.1\r\n", CLIENT),
        (CHAIN, f"x-forwarded-for: 198.51.100.1, {CLIENT}, 10.0.0.5\r\n", CLIENT),
        (TRUSTED, f"x-forwarded-for: 198.51.100.1, {CLIENT}\r\n", CLIENT),
        (
            TRUSTED,
            f"x-forwarded-for: {CLIENT}\r\nx-forwarded-for: 198.51.100.1\r\n",
            PEER,
        ),
        (TRUSTED, "x-forwarded-for: nope\r\n", PEER),
        (TRUSTED, "", PEER),
    ],
)
async def test_the_rate_limit_client_is_the_same_on_both_lanes(
    proxies: tuple[str, ...], extra: str, expected: str
) -> None:
    settings = AppSettings(server=ServerSettings(trusted_proxies=proxies))
    app = Hinoki(include=[where, where_python], settings=settings)
    worker = KisoWorker(app, "127.0.0.1", 0)
    port = await worker.start()
    try:
        lanes = record_lanes(worker)
        native = await fetch(port, "GET", "/where", extra)
        python = await fetch(port, "GET", "/where-python", extra)
    finally:
        await worker.stop()
    assert native.status == python.status == 200
    assert lanes == [True, False]
    assert NATIVE.clients == PYTHON.clients == [expected]
