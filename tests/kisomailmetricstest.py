"""Email metrics on the Rust server's dedicated metrics listener."""

import pytest

from hinoki import Hinoki, http, mail
from hinoki.runtime.kiso import KisoWorker
from hinoki.runtime.listener import open_listener
from hinoki.settings import AppSettings, EmailBackend, EmailSettings, MetricsSettings
from tests.kisometricstest import fetch


class MailSettings(AppSettings):
    email: EmailSettings


@http.get("/send")
async def send() -> dict[str, str]:
    await mail.send(
        mail.Message(
            to=("ada@example.com",), sender="noreply@example.com", subject="s", text="t"
        )
    )
    return {}


async def scrape(app: Hinoki) -> str:
    listener = open_listener("127.0.0.1", 0, 16)
    metrics_port = listener.getsockname()[1]
    options = MetricsSettings(listen=f"127.0.0.1:{metrics_port}").native_options(
        version="test", fd=listener.detach()
    )
    worker = KisoWorker(app, "127.0.0.1", 0, metrics=options)
    port = await worker.start()
    try:
        await fetch(port, "/absent")
        reply = await fetch(port, "/send")
        assert reply.startswith("HTTP/1.1")
        return await fetch(metrics_port, "/metrics")
    finally:
        await worker.stop()


async def test_email_families_are_exposed_when_an_email_module_is_installed() -> None:
    app = Hinoki(
        settings=MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    )
    app.include(send)
    page = await scrape(app)
    assert "# TYPE hinoki_email_messages_total counter" in page
    assert "# TYPE hinoki_email_send_duration_seconds histogram" in page
    assert "# TYPE hinoki_email_retries_total counter" in page
    assert 'backend="memory"' in page
    assert "ada@example.com" not in page


async def test_email_families_are_absent_without_an_email_module() -> None:
    app = Hinoki()
    app.include(send)
    page = await scrape(app)
    assert "hinoki_email_" not in page


@pytest.mark.parametrize("outcome", ["sent"])
async def test_a_send_is_counted_by_outcome(outcome: str) -> None:
    app = Hinoki(
        settings=MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    )
    app.include(send)
    page = await scrape(app)
    assert f'outcome="{outcome}",worker="0"}} 1' in page


@http.get("/send")
async def send_refused() -> dict[str, str]:
    for message in (
        mail.Message(to=("ada@example.com",), subject="s", text="t"),
        mail.Message(to=(), sender="noreply@example.com", subject="s", text="t"),
    ):
        with pytest.raises(mail.EmailError):
            await mail.send(message)
    return {}


async def test_messages_refused_before_sending_are_counted_as_failures() -> None:
    app = Hinoki(
        settings=MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    )
    app.include(send_refused)
    page = await scrape(app)
    assert 'outcome="invalid_message",worker="0"} 2' in page
    assert 'outcome="sent",worker="0"} 1' not in page
