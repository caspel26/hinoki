"""The email module in a running application: handlers, hooks, commands,
after-commit sending and the lifetime of one mailer per worker."""

import asyncio
import subprocess
import sys
import textwrap
from argparse import Namespace
from pathlib import Path

import pytest

from hinoki import Hinoki, http, mail, settings
from hinoki.blocking import run_sync
from hinoki.db import Database, DatabaseModule
from hinoki.management import Command, CommandContext
from hinoki.orm import current_database
from hinoki.runtime.management import run_command
from hinoki.settings import EmailBackend, EmailSettings
from hinoki.testing import ASGIClient

ROOT = Path(__file__).resolve().parent.parent


class MailSettings(settings.AppSettings):
    email: EmailSettings


def memory_settings() -> MailSettings:
    return MailSettings(
        email=EmailSettings(
            backend=EmailBackend.MEMORY, default_sender="noreply@example.com"
        )
    )


def email_module(app: Hinoki) -> mail.EmailModule:
    return next(m for m in app.modules if isinstance(m, mail.EmailModule))


def note(subject: str) -> mail.Message:
    return mail.Message(to=("ada@example.com",), subject=subject, text="body")


@http.post("/async-send")
async def async_send() -> dict[str, str]:
    sent = await mail.send(note("from an async handler"))
    return {"id": sent.message_id}


@http.post("/sync-send")
def sync_send() -> dict[str, str]:
    sent = run_sync(mail.send(note("from a sync handler")))
    return {"id": sent.message_id}


@http.post("/bad-send")
async def bad_send() -> dict[str, str]:
    await mail.send(
        mail.Message(to=("not an address",), subject="private subject", text="x")
    )
    return {}


def app_with(*routes: object, **options: object) -> Hinoki:
    app = Hinoki(settings=memory_settings(), **options)  # type: ignore[arg-type]
    app.include(*routes)  # type: ignore[arg-type]
    return app


async def test_async_and_sync_handlers_send_through_the_workers_mailer() -> None:
    app = app_with(async_send, sync_send)
    async with ASGIClient(app) as client:
        first = await client.post("/async-send")
        second = await client.post("/sync-send")
        assert first.status == second.status == 200
        outbox = email_module(app).mailer.outbox
        assert [sent.message_id for sent in outbox] == [
            first.body["id"],
            second.body["id"],
        ]
        assert [sent.message.subject for sent in outbox if sent.message] == [
            "from an async handler",
            "from a sync handler",
        ]


async def test_an_unhandled_email_error_is_a_500_that_leaks_nothing() -> None:
    app = app_with(bad_send)
    async with ASGIClient(app) as client:
        reply = await client.post("/bad-send")
        assert reply.status == 500
        assert "private" not in reply.raw.decode()
        assert "address" not in reply.raw.decode()


async def test_the_mailer_is_closed_when_the_worker_stops() -> None:
    app = app_with()
    async with ASGIClient(app):
        mailer = email_module(app).mailer
        await mailer.send(note("while running"))
    with pytest.raises(mail.EmailError) as caught:
        await mailer.send(note("after shutdown"))
    assert caught.value.kind is mail.EmailErrorKind.CLOSED
    with pytest.raises(RuntimeError, match="not running"):
        _ = email_module(app).mailer


async def test_two_workers_in_one_process_each_own_a_mailer_and_an_outbox() -> None:
    first, second = app_with(async_send), app_with(async_send)
    async with ASGIClient(first) as one, ASGIClient(second) as two:
        await one.post("/async-send")
        await one.post("/async-send")
        await two.post("/async-send")
        mailers = (email_module(first).mailer, email_module(second).mailer)
        assert mailers[0] is not mailers[1]
        assert [len(m.outbox) for m in mailers] == [2, 1]
    assert mail.mailer_var.get() is None


async def test_one_application_started_twice_opens_a_fresh_mailer_each_time() -> None:
    app = app_with(async_send)
    seen = []
    for _ in range(2):
        async with ASGIClient(app) as client:
            await client.post("/async-send")
            seen.append(email_module(app).mailer)
            assert len(seen[-1].outbox) == 1
    assert seen[0] is not seen[1]


async def test_a_startup_hook_can_send() -> None:
    sent: list[mail.SentMail] = []

    async def hook(_ctx: object) -> None:
        sent.append(await mail.send(note("from a hook")))

    app = Hinoki(settings=memory_settings(), on_startup=[hook])
    async with ASGIClient(app):
        assert len(sent) == 1
        assert email_module(app).mailer.outbox == (sent[0],)


class Greeting(Command):
    name = "greet"
    requires = (mail.EmailModule,)

    async def handle(self, context: CommandContext, _args: Namespace) -> int:
        sent = await mail.send(note("from a command"))
        assert context.get(mail.Mailer).outbox == (sent,)
        return 0


def test_a_management_command_can_send_and_the_mailer_is_closed_after() -> None:
    app = Hinoki(settings=memory_settings(), commands=[Greeting])
    assert asyncio.run(run_command(app, app.commands[0], Namespace())) == 0
    assert mail.mailer_var.get() is None


@pytest.fixture
def table(tmp_path: Path) -> str:
    return f"sqlite://{tmp_path / 'mail.sqlite3'}"


async def prepare(url: str) -> None:
    database = await Database.connect(url, max_size=2)
    await database.execute("CREATE TABLE invites (email text PRIMARY KEY)")
    database.close()


@http.post("/invite/{email}/{outcome}")
async def invite(email: str, outcome: str) -> dict[str, str]:
    database = current_database()
    try:
        async with database.atomic():
            await database.execute("INSERT INTO invites (email) VALUES (?)", email)
            database.after_commit(lambda: mail.send(note(f"invite {email}")))
            if outcome == "fail":
                raise ZeroDivisionError
    except ZeroDivisionError:
        return {"state": "rolled back"}
    return {"state": "committed"}


async def test_mail_queued_with_after_commit_is_sent_on_commit_and_never_on_rollback(
    table: str,
) -> None:
    await prepare(table)
    app = Hinoki(settings=memory_settings(), modules=[DatabaseModule(table)])
    app.include(invite)
    async with ASGIClient(app) as client:
        kept = await client.post("/invite/ada/ok")
        dropped = await client.post("/invite/grace/fail")
        assert (kept.body["state"], dropped.body["state"]) == (
            "committed",
            "rolled back",
        )
        subjects = [
            sent.message.subject
            for sent in email_module(app).mailer.outbox
            if sent.message
        ]
        assert subjects == ["invite ada"]


async def test_a_failing_after_commit_send_does_not_undo_the_commit(
    table: str, caplog: pytest.LogCaptureFixture
) -> None:
    await prepare(table)

    @http.post("/invite-bad")
    async def invite_bad() -> dict[str, str]:
        database = current_database()
        async with database.atomic():
            await database.execute("INSERT INTO invites (email) VALUES ('kept')")
            database.after_commit(
                lambda: mail.send(
                    mail.Message(to=("private.person@",), subject="s", text="t")
                )
            )
        return {"state": "done"}

    app = Hinoki(settings=memory_settings(), modules=[DatabaseModule(table)])
    app.include(invite_bad)
    async with ASGIClient(app) as client:
        reply = await client.post("/invite-bad")
        assert reply.status == 200
        rows = await current_database_rows(app)
        assert rows == ["kept"]
    assert "private.person" not in caplog.text


async def current_database_rows(app: Hinoki) -> list[str]:
    module = next(m for m in app.modules if isinstance(m, DatabaseModule))
    database = await Database.connect(module.url, max_size=1)
    try:
        return [
            row["email"] for row in await database.fetch("SELECT email FROM invites")
        ]
    finally:
        database.close()


def test_importing_the_mail_module_does_no_io_and_opens_no_mailer() -> None:
    program = textwrap.dedent(
        """
        import asyncio, socket, ssl

        def refuse(*args, **kwargs):
            raise AssertionError("network used at import")

        socket.create_connection = refuse
        socket.getaddrinfo = refuse
        from hinoki import mail, settings

        assert mail.mailer_var.get() is None
        mail.EmailModule(settings.EmailSettings(backend="smtp", host="mx.invalid"))
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        check=False,
    )
    assert result.stdout.strip() == "ok", result.stderr
