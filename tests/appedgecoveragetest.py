"""Application registration and protocol boundary fallback cases."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import pytest

from hinoki import Hinoki, RouteGroup, http
from hinoki.errors import RegistrationError


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def test_hook_validation_accepts_partials_and_rejects_bad_values() -> None:
    def hook(_context: object) -> None:
        return None

    app = Hinoki(on_startup=[partial(hook)])
    assert len(app.on_startup) == 1
    with pytest.raises(RegistrationError, match="takes a list"):
        Hinoki(on_startup=hook)  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="is not a function"):
        Hinoki(on_startup=[None])  # type: ignore[list-item]


@pytest.mark.parametrize("malformed", ["empty", "undecorated"])
def test_route_group_registration_reports_malformed_groups(malformed: str) -> None:
    group = RouteGroup("/group")
    if malformed == "undecorated":
        group._handlers.append(lambda: None)
    with pytest.raises(RegistrationError, match="RouteGroup"):
        Hinoki(include=[group])


def test_application_lifespan_reports_late_registration_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki(include=[health])

    def fail_binding() -> Any:
        raise RegistrationError("invalid route binding")

    monkeypatch.setattr(app, "asgi", fail_binding)
    received = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        nonlocal received
        received = True
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            receive,
            send,
        )
    )
    assert received
    assert sent[0]["type"] == "lifespan.startup.failed"

    async def request_scope() -> None:
        with pytest.raises(RegistrationError, match="invalid route binding"):
            await app({"type": "http"}, receive, send)

    asyncio.run(request_scope())


def test_application_rejects_an_unexpected_native_router_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Hinoki(include=[health])
    monkeypatch.setattr(
        "hinoki.app.Router.find", lambda _router, _method, _path: (None, None)
    )
    with pytest.raises(RuntimeError, match="unexpected router answer"):
        app.lookup("GET", "/health")
