"""What a deprecated action tells its callers: a header and a bounded warning."""

import logging
from datetime import UTC, date, datetime

import pytest

from hinoki import Hinoki, http
from hinoki.cli.routes import lanes_of
from hinoki.http.request import Request
from hinoki.runtime import deprecation
from hinoki.runtime.deprecation import Reminders, header_of
from hinoki.testing import ASGIClient


@http.get("/old", deprecated=True)
async def old(request: Request) -> dict[str, str]:
    return {"v": "old"}


@http.get("/dated", deprecated=date(2026, 11, 22))
async def dated(request: Request) -> dict[str, str]:
    return {"v": "dated"}


@http.get("/current")
async def current(request: Request) -> dict[str, str]:
    return {"v": "current"}


def test_header_forms() -> None:
    assert header_of(False) is None
    assert header_of(True) == ("deprecation", "true")
    stamp = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp())
    assert header_of(date(2026, 11, 22)) == ("deprecation", f"@{stamp}")


async def test_only_deprecated_actions_carry_the_header() -> None:
    app = Hinoki()
    app.include(old, dated, current)
    stamp = int(datetime(2026, 11, 22, tzinfo=UTC).timestamp())
    async with ASGIClient(app) as client:
        assert (await client.get("/old")).headers["deprecation"] == "true"
        assert (await client.get("/dated")).headers["deprecation"] == f"@{stamp}"
        reply = await client.get("/current")
    assert "deprecation" not in reply.headers
    document = app.openapi()["paths"]
    assert document["/dated"]["get"]["deprecated"] is True


async def test_a_call_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    app = Hinoki()
    app.include(old, current)
    with caplog.at_level(logging.WARNING, logger="hinoki.deprecated"):
        async with ASGIClient(app) as client:
            await client.get("/current")
            await client.get("/old")
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "deprecated action called: GET /old (1 call(s) since the last report)"
    ]


def test_reports_are_bounded_per_action(caplog: pytest.LogCaptureFixture) -> None:
    app = Hinoki()
    app.include(old, current)
    route = next(r for r in app.routes if r.deprecated)
    reminders = Reminders(app.routes)
    every = deprecation.REPORT_EVERY
    with caplog.at_level(logging.WARNING, logger="hinoki.deprecated"):
        for moment in (100.0, 101.0, 102.0, 100.0 + every):
            reminders.called(route, moment)
        reminders.called(next(r for r in app.routes if not r.deprecated), 500.0)
    assert [r.getMessage() for r in caplog.records] == [
        "deprecated action called: GET /old (1 call(s) since the last report)",
        "deprecated action called: GET /old (3 call(s) since the last report)",
    ]


@http.get("/plain")
async def plain() -> dict[str, int]:
    return {"n": 1}


@http.get("/plain-old", deprecated=True)
async def plain_old() -> dict[str, int]:
    return {"n": 1}


@http.get("/picky-old", deprecated=True)
async def picky_old(n: int) -> dict[str, int]:
    return {"n": n}


def test_a_deprecated_action_leaves_the_native_lane() -> None:
    app = Hinoki()
    app.include(plain, plain_old)
    lanes = {route.path: lanes_of(app)[id(route)] for route in app.routes}
    assert lanes["/plain"] == ("native", "native_dispatch")
    assert lanes["/plain-old"] == ("python", "deprecated")


async def test_a_deprecated_action_served_without_request_has_the_header() -> None:
    app = Hinoki()
    app.include(plain_old, picky_old)
    async with ASGIClient(app) as client:
        reply = await client.get("/plain-old")
        failed = await client.get("/picky-old")
    assert (reply.status, reply.headers["deprecation"]) == (200, "true")
    assert (failed.status, failed.headers["deprecation"]) == (422, "true")


@pytest.mark.parametrize("value", ["yes", 1, datetime(2026, 1, 1, tzinfo=UTC)])
def test_deprecated_takes_a_flag_or_a_date(value: object) -> None:
    with pytest.raises(ValueError, match="takes True, False or a date"):
        http.get("/x", deprecated=value)(lambda: None)  # type: ignore[arg-type]
