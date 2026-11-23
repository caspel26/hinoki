"""The smtp backend against a fake server on the loopback: delivery, TLS, AUTH,
retries, timeouts, the connection pool and the errors each failure raises."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email import message_from_bytes, policy

import pytest

from hinoki import Hinoki, mail, settings
from hinoki.settings import EmailSettings
from hinoki.testing import ASGIClient
from tests.mailserver import CERTIFICATE, CLOSE, STALL, FakeSMTP

PASSWORD = "synthetic-pw-4d7e1c"
Kind = mail.EmailErrorKind


def smtp_settings(server: FakeSMTP, **options: object) -> EmailSettings:
    defaults: dict[str, object] = {
        "backend": "smtp",
        "host": "127.0.0.1",
        "port": server.port,
        "security": "none",
        "default_sender": "noreply@example.com",
        "backoff_base": 0.01,
        "backoff_max": 0.02,
        "command_timeout": 2.0,
        "connect_timeout": 2.0,
        "timeout": 8.0,
    }
    return EmailSettings(**{**defaults, **options})  # type: ignore[arg-type]


@asynccontextmanager
async def running(server: FakeSMTP, **options: object) -> AsyncIterator[mail.Mailer]:
    mailer = mail.Mailer(smtp_settings(server, **options))
    try:
        yield mailer
    finally:
        await mailer.aclose()


def hello(**fields: object) -> mail.Message:
    fields.setdefault("to", ("ada@example.com",))
    fields.setdefault("subject", "Hello")
    fields.setdefault("text", "Body")
    return mail.Message(**fields)  # type: ignore[arg-type]


async def failure(mailer: mail.Mailer, message: mail.Message) -> mail.EmailError:
    with pytest.raises(mail.EmailError) as caught:
        await mailer.send(message)
    return caught.value


async def test_a_message_is_delivered_with_its_envelope_and_a_dot_stuffed_body() -> (
    None
):
    async with FakeSMTP() as server, running(server) as mailer:
        text = "first\n.starts with a dot\n..two\nlast"
        sent = await mailer.send(
            hello(to=("ada@example.com",), bcc=("hidden@example.com",), text=text)
        )
        assert sent.raw is None and sent.message is None and sent.attempts == 1
        [received] = server.messages
        assert received.sender == "noreply@example.com"
        assert received.recipients == ("ada@example.com", "hidden@example.com")
        parsed = message_from_bytes(received.data, policy=policy.default)
        assert parsed["Bcc"] is None and parsed["Message-ID"] == sent.message_id
        body = parsed.get_body()
        assert body is not None
        assert body.get_content().strip() == text.replace("\n", "\r\n")
        assert mailer.stats()["sent"] == 1 and mailer.stats()["failed"] == 0


async def test_starttls_then_auth_plain_over_the_upgraded_connection() -> None:
    async with FakeSMTP(tls=True) as server:
        async with running(
            server,
            security="starttls",
            tls_ca_file=str(CERTIFICATE),
            username="user",
            password=PASSWORD,
        ) as mailer:
            await mailer.send(hello())
        order = [line.split(" ")[0] for line in server.commands]
        assert order[:5] == ["EHLO", "STARTTLS", "EHLO", "AUTH", "MAIL"]
        assert len(server.messages) == 1


async def test_implicit_tls_with_a_trusted_certificate() -> None:
    async with FakeSMTP(implicit_tls=True) as server:
        async with running(
            server,
            security="tls",
            tls_ca_file=str(CERTIFICATE),
            username="user",
            password=PASSWORD,
        ) as mailer:
            await mailer.send(hello())
        assert "STARTTLS" not in server.commands and len(server.messages) == 1


async def test_an_untrusted_certificate_is_a_tls_error_and_nothing_is_sent() -> None:
    async with FakeSMTP(implicit_tls=True) as server:
        async with running(server, security="tls", retries=2) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.TLS and str(error) == (
            "TLS negotiation with the SMTP server failed"
        )
        assert server.messages == [] and server.commands == []
        assert error.attempts == 1


async def test_starttls_that_the_server_does_not_offer_is_refused() -> None:
    async with FakeSMTP(credentials=None) as server:
        async with running(server, security="starttls") as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.TLS and server.messages == []
        assert server.saw("MAIL") == 0


@pytest.mark.parametrize("mechanisms", ["PLAIN LOGIN", "LOGIN"])
async def test_auth_uses_plain_and_falls_back_to_login(mechanisms: str) -> None:
    async with FakeSMTP(mechanisms=mechanisms) as server:
        async with running(
            server, username="user", password=PASSWORD, allow_insecure=True
        ) as mailer:
            await mailer.send(hello())
        assert len(server.messages) == 1 and server.saw("AUTH") == 1


async def test_wrong_credentials_are_an_auth_error_that_is_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeSMTP() as server:
        async with running(
            server, username="user", password="wrong-" + PASSWORD, retries=3
        ) as mailer:
            with caplog.at_level(logging.DEBUG):
                error = await failure(mailer, hello())
            assert mailer.stats()["failed"] == 1
        assert error.kind is Kind.AUTH and error.code == 535
        assert error.enhanced_status == "5.7.8" and error.attempts == 1
        assert server.connections == 1 and server.messages == []
        shown = " ".join([str(error), repr(error), repr(mailer), caplog.text])
        assert PASSWORD not in shown and "user" not in shown


async def test_a_server_without_auth_refuses_credentials_locally() -> None:
    async with FakeSMTP(credentials=None) as server:
        async with running(server, username="user", password=PASSWORD) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.AUTH and server.messages == []


async def test_a_greeting_that_defers_is_retried_and_counted() -> None:
    async with FakeSMTP() as server:
        server.script["GREETING"].append("421 4.3.2 try later")
        async with running(server, retries=2) as mailer:
            sent = await mailer.send(hello())
            assert sent.attempts == 2
            assert mailer.stats()["retries"] == 1 and mailer.stats()["sent"] == 1
        assert server.connections == 2 and len(server.messages) == 1


async def test_a_deferral_after_data_is_retried_without_duplicates() -> None:
    async with FakeSMTP() as server:
        server.script["END"].append("451 4.3.0 local error")
        async with running(server, retries=2) as mailer:
            sent = await mailer.send(hello())
        assert sent.attempts == 2 and len(server.messages) == 2  # the first was refused


async def test_deferrals_beyond_the_retries_are_a_temporary_error() -> None:
    async with FakeSMTP() as server:
        server.script["MAIL"].extend(["451 4.7.1 later"] * 5)
        async with running(server, retries=1) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.TEMPORARY and error.code == 451
        assert error.attempts == 2 and mailer.stats()["retries"] == 1
        assert str(error) == "the SMTP server deferred the message (code 451)"


async def test_a_rejected_recipient_fails_the_whole_message_with_a_reset() -> None:
    async with FakeSMTP() as server:
        server.script["RCPT"].extend(["250 ok", "550 5.1.1 no such user private-name"])
        async with running(server, retries=3) as mailer:
            error = await failure(
                mailer, hello(to=("ada@example.com", "ghost@example.com"))
            )
            await mailer.send(hello())  # the mailer is still usable
        assert error.kind is Kind.REJECTED and error.code == 550
        assert error.enhanced_status == "5.1.1" and error.attempts == 1
        assert "private-name" not in str(error) + repr(error)
        assert "ghost" not in str(error) + repr(error)
        assert server.saw("RSET") >= 1 and server.saw("DATA") == 1
        assert [m.recipients for m in server.messages] == [("ada@example.com",)]


async def test_the_server_size_limit_is_checked_before_data() -> None:
    async with FakeSMTP(size=2000) as server:
        async with running(server) as mailer:
            error = await failure(mailer, hello(text="x" * 5000))
        assert error.kind is Kind.LIMIT_EXCEEDED
        assert server.saw("DATA") == 0


async def test_an_oversized_reply_is_a_protocol_error() -> None:
    async with FakeSMTP() as server:
        server.script["MAIL"].extend(["250 " + "x" * 5000] * 5)
        async with running(server, retries=0) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.PROTOCOL and server.messages == []
        assert "xxxx" not in str(error)


async def test_a_stalled_greeting_times_out_and_the_retry_succeeds() -> None:
    async with FakeSMTP() as server:
        server.script["GREETING"].append(STALL)
        async with running(server, retries=1, command_timeout=0.3) as mailer:
            sent = await mailer.send(hello())
        assert sent.attempts == 2


async def test_a_stall_without_retries_is_a_timeout() -> None:
    async with FakeSMTP() as server:
        server.script["EHLO"].append(STALL)
        async with running(server, retries=0, command_timeout=0.2) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.TIMEOUT and error.possibly_sent is False
        assert str(error) == "SMTP operation timed out"


async def test_silence_after_the_message_end_is_possibly_sent_and_not_retried() -> None:
    async with FakeSMTP() as server:
        server.script["END"].append(STALL)
        async with running(server, retries=3, data_timeout=0.3) as mailer:
            error = await failure(mailer, hello())
        assert error.kind is Kind.TIMEOUT and error.possibly_sent is True
        assert error.attempts == 1 and len(server.messages) == 1
        assert server.connections == 1


async def test_the_overall_timeout_bounds_one_send() -> None:
    async with FakeSMTP() as server:
        server.script["GREETING"].extend([STALL] * 5)
        async with running(
            server, retries=5, timeout=0.5, connect_timeout=0.3, command_timeout=0.3
        ) as mailer:
            started = asyncio.get_running_loop().time()
            error = await failure(mailer, hello())
            assert asyncio.get_running_loop().time() - started < 3
        assert error.kind is Kind.TIMEOUT


async def test_a_dropped_connection_is_retried() -> None:
    async with FakeSMTP() as server:
        server.script["MAIL"].append(CLOSE)
        async with running(server, retries=1) as mailer:
            sent = await mailer.send(hello())
        assert sent.attempts == 2


async def test_a_refused_connection_is_a_connect_error() -> None:
    async with FakeSMTP() as server:
        pass
    mailer = mail.Mailer(smtp_settings(server, retries=1))
    try:
        error = await failure(mailer, hello())
    finally:
        await mailer.aclose()
    assert error.kind is Kind.CONNECT and error.attempts == 2
    assert str(error) == "could not connect to the SMTP server"


async def test_connections_are_pooled_and_closed_with_a_quit() -> None:
    async with FakeSMTP() as server:
        async with running(server) as mailer:
            for _ in range(3):
                await mailer.send(hello())
            assert server.connections == 1
            assert mailer.stats()["connections_open"] == 1
            assert mailer.stats()["connections_idle"] == 1
        assert server.saw("QUIT") == 1
        assert mailer.stats()["connections_open"] == 0


async def test_a_connection_is_renewed_after_its_message_budget() -> None:
    async with FakeSMTP() as server:
        async with running(server, max_messages_per_connection=2) as mailer:
            for _ in range(5):
                await mailer.send(hello())
        assert server.connections == 3


async def test_a_full_pool_is_busy_and_cancelling_a_send_frees_its_slot() -> None:
    async with FakeSMTP() as server:
        server.script["MAIL"].append(STALL)
        async with running(
            server, max_connections=1, pool_wait=0.1, command_timeout=5.0, timeout=10.0
        ) as mailer:
            holder = asyncio.create_task(mailer.send(hello()))
            await asyncio.sleep(0.3)
            error = await failure(mailer, hello())
            assert error.kind is Kind.BUSY
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder
            await asyncio.sleep(0.1)
            sent = await mailer.send(hello())
            assert sent.attempts == 1 and len(server.messages) == 1


async def test_closing_refuses_new_sends() -> None:
    async with FakeSMTP() as server:
        async with running(server) as mailer:
            await mailer.send(hello())
            await mailer.aclose()
            error = await failure(mailer, hello())
        assert error.kind is Kind.CLOSED and server.saw("QUIT") == 1


async def test_check_connects_and_authenticates_without_sending() -> None:
    async with FakeSMTP(tls=True) as server:
        async with running(
            server,
            security="starttls",
            tls_ca_file=str(CERTIFICATE),
            username="user",
            password=PASSWORD,
        ) as mailer:
            await mailer.check()
        assert server.messages == [] and server.saw("QUIT") == 1
        assert server.saw("AUTH") == 1


async def test_check_reports_a_failure_with_its_kind() -> None:
    async with FakeSMTP() as server:
        async with running(server, username="user", password="bad") as mailer:
            with pytest.raises(mail.EmailError) as caught:
                await mailer.check()
        assert caught.value.kind is Kind.AUTH


class SmtpSettings(settings.AppSettings, prefix="Y_"):
    email: EmailSettings


async def test_check_on_startup_aborts_the_start_when_the_server_refuses() -> None:
    async with FakeSMTP() as server:
        config = smtp_settings(
            server, username="user", password="bad", check_on_startup=True, retries=0
        )
        app = Hinoki(settings=SmtpSettings(email=config))
        with pytest.raises(RuntimeError, match="startup failed"):
            async with ASGIClient(app):
                pass
        assert server.messages == []


async def test_check_on_startup_passes_with_a_working_server() -> None:
    async with FakeSMTP() as server:
        config = smtp_settings(
            server,
            username="user",
            password=PASSWORD,
            check_on_startup=True,
            allow_insecure=True,
        )
        app = Hinoki(settings=SmtpSettings(email=config))
        async with ASGIClient(app):
            module = next(m for m in app.modules if isinstance(m, mail.EmailModule))
            await module.mailer.send(hello())
        assert len(server.messages) == 1
