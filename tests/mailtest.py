"""The email API over the memory and console backends: rendering, validation,
limits and errors that never carry recipients or content."""

import copy
import pickle
from collections.abc import Callable
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from hinoki import mail
from hinoki.settings import EmailBackend, EmailSettings

SENDER = "noreply@example.com"
SECRET_ADDRESS = "private.person@example.com"
SECRET_SUBJECT = "private subject text"
SECRET_BODY = "private body text"


def memory(**options: object) -> mail.Mailer:
    options.setdefault("default_sender", SENDER)
    return mail.Mailer(EmailSettings(backend="memory", **options))  # type: ignore[arg-type]


def parse(sent: mail.SentMail) -> EmailMessage:
    assert sent.raw is not None
    parsed = message_from_bytes(sent.raw, policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def basic(**fields: object) -> mail.Message:
    fields.setdefault("to", ("ada@example.com",))
    fields.setdefault("subject", "Hello")
    fields.setdefault("text", "Body")
    return mail.Message(**fields)  # type: ignore[arg-type]


async def refused(mailer: mail.Mailer, message: mail.Message) -> mail.EmailError:
    with pytest.raises(mail.EmailError) as caught:
        await mailer.send(message)
    return caught.value


async def test_a_message_is_rendered_and_kept_in_the_outbox() -> None:
    mailer = memory()
    sent = await mailer.send(
        basic(
            to=("ada@example.com", mail.Address("grace@example.com", "Grace Hopper")),
            cc=("cc@example.com",),
            reply_to=("replies@example.com",),
            subject="Welcome",
            text="plain",
            html="<p>rich</p>",
            headers=(("X-Campaign", "spring"),),
        )
    )
    parsed = parse(sent)
    assert parsed["From"] == SENDER
    assert parsed["To"] == "ada@example.com, Grace Hopper <grace@example.com>"
    assert parsed["Cc"] == "cc@example.com"
    assert parsed["Reply-To"] == "replies@example.com"
    assert parsed["Subject"] == "Welcome"
    assert parsed["X-Campaign"] == "spring"
    assert parsed["Message-ID"] == sent.message_id
    assert sent.message_id.endswith("@example.com>")
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None and body.get_content().strip() == "plain"
    rich = parsed.get_body(preferencelist=("html",))
    assert rich is not None and "<p>rich</p>" in rich.get_content()
    assert sent.recipients == (
        "ada@example.com",
        "grace@example.com",
        "cc@example.com",
    )
    assert sent.attempts == 1
    assert mailer.outbox == (sent,)
    assert sent.message is not None and sent.message.subject == "Welcome"


async def test_bcc_is_an_envelope_recipient_and_never_a_header() -> None:
    sent = await memory().send(basic(bcc=("hidden@example.com",)))
    assert sent.recipients == ("ada@example.com", "hidden@example.com")
    assert sent.raw is not None and b"hidden@example.com" not in sent.raw
    assert parse(sent)["Bcc"] is None


async def test_attachments_and_non_ascii_text_round_trip() -> None:
    content = bytes(range(256)) * 3
    sent = await memory().send(
        basic(
            subject="Résumé",
            text="Grüße",
            attachments=(
                mail.Attachment("données.bin", content, "application/x-test"),
            ),
        )
    )
    parsed = parse(sent)
    assert parsed["Subject"] == "Résumé"
    parts = list(parsed.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "donn_es.bin"  # the ASCII fallback
    assert sent.raw is not None and b"filename*=utf-8''donn%C3%A9es.bin" in sent.raw
    assert parts[0].get_content_type() == "application/x-test"
    assert parts[0].get_content() == content
    body = parsed.get_body()
    assert body is not None and body.get_content().strip() == "Grüße"


async def test_the_default_sender_is_used_unless_the_message_sets_one() -> None:
    mailer = memory()
    own = await mailer.send(basic(sender=mail.Address("team@example.com", "Team")))
    assert parse(own)["From"] == "Team <team@example.com>"
    assert parse(await mailer.send(basic()))["From"] == SENDER


async def test_a_message_with_no_sender_and_no_default_names_the_sender_field() -> None:
    error = await refused(memory(default_sender=None), basic())
    assert error.kind is mail.EmailErrorKind.INVALID_MESSAGE
    assert error.field == "sender"


async def test_the_message_id_domain_follows_the_settings() -> None:
    explicit = await memory(message_id_domain="ids.example.org").send(basic())
    assert explicit.message_id.endswith("@ids.example.org>")
    helo = await memory(default_sender=None, helo_name="mx.example.org").send(
        basic(sender="a@example.com")
    )
    assert helo.message_id.endswith("@mx.example.org>")
    bare = await memory(default_sender=None).send(basic(sender="a@example.com"))
    assert bare.message_id.endswith("@localhost>")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("to", ("plain",)),
        ("to", ("a@b",)),
        ("to", ("a b@example.com",)),
        ("to", ("Ada <ada@example.com>",)),
        ("to", ("ada@example.com\r\nBcc: x@example.com",)),
        ("to", ("",)),
        ("to", ("ada@@example.com",)),
        ("to", ("ada@exa mple.com",)),
        ("to", ("ada@[127.0.0.1]",)),
        ("to", ("ada@-example.com",)),
        ("to", ("ada@" + "a" * 64 + ".example.com",)),
        ("to", (123,)),
        ("to", (mail.Address("ada@example.com", 5),)),  # type: ignore[arg-type]
        ("cc", ("bad",)),
        ("bcc", ("bad",)),
        ("reply_to", ("bad",)),
    ],
)
async def test_an_invalid_address_names_its_field_and_position(
    field: str, value: tuple[object, ...]
) -> None:
    valid = "ok@example.com"
    message = basic(**{field: (valid, *value)})
    error = await refused(memory(), message)
    assert error.kind is mail.EmailErrorKind.INVALID_ADDRESS
    assert (error.field, error.index) == (field, 1)
    assert str(error) == f"invalid email address in {field}[1]"


async def test_an_invalid_sender_has_no_position() -> None:
    error = await refused(memory(), basic(sender="bad"))
    assert error.kind is mail.EmailErrorKind.INVALID_ADDRESS
    assert (error.field, error.index) == ("sender", None)


async def test_an_internationalized_domain_is_normalized_to_ascii() -> None:
    sent = await memory().send(basic(to=("ada@bücher.example",)))
    assert sent.recipients == ("ada@xn--bcher-kva.example",)


@pytest.mark.parametrize(
    ("make", "field"),
    [
        (lambda: basic(subject="a\r\nBcc: evil@example.com"), "subject"),
        (lambda: basic(subject="a\nb"), "subject"),
        (lambda: basic(subject="a\x00b"), "subject"),
        (lambda: basic(headers=(("X-A", "v\r\nBcc: evil@example.com"),)), "headers"),
        (lambda: basic(headers=(("X-A\r\nBcc", "v"),)), "headers"),
        (lambda: basic(headers=(("From", "evil@example.com"),)), "headers"),
        (lambda: basic(headers=(("message-id", "<x@example.com>"),)), "headers"),
        (lambda: basic(headers=(("X-A", "v", "extra"),)), "headers"),
        (lambda: basic(headers=((b"X-A", "v"),)), "headers"),
        (
            lambda: basic(
                to=(mail.Address("ada@example.com", "Ada\r\nBcc: evil@example.com"),)
            ),
            "to",
        ),
        (
            lambda: basic(
                attachments=(mail.Attachment("a\r\nb.txt", b"x", "text/plain"),)
            ),
            "attachments",
        ),
        (
            lambda: basic(
                attachments=(mail.Attachment("a.txt", b"x", "text/plain\r\nX: y"),)
            ),
            "attachments",
        ),
    ],
)
async def test_header_injection_is_refused(
    make: Callable[[], mail.Message], field: str
) -> None:
    mailer = memory()
    error = await refused(mailer, make())
    assert error.kind in {
        mail.EmailErrorKind.INVALID_HEADER,
        mail.EmailErrorKind.INVALID_MESSAGE,
    }
    assert error.field == field
    assert mailer.outbox == ()
    assert "evil" not in str(error)


@pytest.mark.parametrize(
    ("make", "field"),
    [
        (lambda: basic(text=None), None),
        (lambda: basic(to=()), None),
        (lambda: basic(text="a\x00b"), "text"),
        (lambda: basic(html="a\x00b"), "html"),
        (lambda: basic(attachments=("not an attachment",)), "attachments"),
    ],
)
async def test_an_incomplete_or_malformed_message_is_refused(
    make: Callable[[], mail.Message], field: str | None
) -> None:
    error = await refused(memory(), make())
    assert error.kind is mail.EmailErrorKind.INVALID_MESSAGE
    assert field is None or error.field == field


async def test_the_limits_refuse_before_anything_is_rendered() -> None:
    few = memory(max_recipients=2, max_attachments=1, max_message_bytes=2048)
    over = await refused(
        few, basic(to=("a@example.com", "b@example.com", "c@example.com"))
    )
    assert over.kind is mail.EmailErrorKind.LIMIT_EXCEEDED
    two = (mail.Attachment("a.txt", b"x"), mail.Attachment("b.txt", b"y"))
    count = await refused(few, basic(attachments=two))
    assert (count.kind, count.field) == (
        mail.EmailErrorKind.LIMIT_EXCEEDED,
        "attachments",
    )
    big = await refused(
        few, basic(attachments=(mail.Attachment("a.bin", b"x" * 4096),))
    )
    assert big.kind is mail.EmailErrorKind.LIMIT_EXCEEDED
    large_text = await refused(few, basic(text="x" * 4096))
    assert large_text.kind is mail.EmailErrorKind.LIMIT_EXCEEDED
    assert few.outbox == ()
    assert few.stats()["failed"] == 4
    assert few.stats()["sent"] == 0


async def test_errors_and_reprs_never_carry_addresses_or_content() -> None:
    mailer = memory()
    message = basic(
        to=("ok@example.com", SECRET_ADDRESS + "\r\n"),
        subject=SECRET_SUBJECT,
        text=SECRET_BODY,
    )
    error = await refused(mailer, message)
    shown = " ".join([str(error), repr(error), repr(error.args), repr(mailer)])
    for secret in (SECRET_ADDRESS, SECRET_SUBJECT, SECRET_BODY, "private"):
        assert secret not in shown
    assert error.__cause__ is None and error.__context__ is None


def test_an_error_has_a_fixed_sentence_per_kind() -> None:
    Kind = mail.EmailErrorKind
    assert str(mail.EmailError(Kind.REJECTED, code=550)) == (
        "the SMTP server rejected the message (code 550)"
    )
    assert (
        str(mail.EmailError(Kind.TEMPORARY)) == "the SMTP server deferred the message"
    )
    assert str(mail.EmailError(Kind.TIMEOUT)) == "SMTP operation timed out"
    assert str(mail.EmailError(Kind.LIMIT_EXCEEDED, field="text")) == (
        "message exceeds the text limit"
    )
    assert str(mail.EmailError(Kind.INVALID_ADDRESS)) == (
        "invalid email address in message"
    )
    assert str(mail.EmailError(Kind.INVALID_ADDRESS, field="to")) == (
        "invalid email address in to"
    )
    assert str(mail.EmailError(Kind.INVALID_HEADER, field="headers")) == (
        "invalid header value in headers"
    )
    assert len({str(mail.EmailError(kind, code=1)) for kind in Kind}) == len(Kind)


def test_an_error_survives_pickle_and_copy() -> None:
    error = mail.EmailError(
        mail.EmailErrorKind.REJECTED,
        code=550,
        enhanced_status="5.1.1",
        attempts=2,
        possibly_sent=True,
        field="to",
        index=3,
    )
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error)):
        assert clone.kind is mail.EmailErrorKind.REJECTED
        assert (clone.code, clone.enhanced_status, clone.attempts) == (550, "5.1.1", 2)
        assert (clone.possibly_sent, clone.field, clone.index) == (True, "to", 3)
        assert str(clone) == str(error)


async def test_the_outbox_keeps_the_newest_messages_up_to_its_capacity() -> None:
    mailer = memory(memory_capacity=2)
    ids = [(await mailer.send(basic(subject=f"n{n}"))).message_id for n in range(3)]
    assert [sent.message_id for sent in mailer.outbox] == ids[1:]
    mailer.clear_outbox()
    assert mailer.outbox == ()
    assert mailer.stats()["sent"] == 3


async def test_the_console_backend_prints_the_message_and_keeps_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mailer = mail.Mailer(
        EmailSettings(backend=EmailBackend.CONSOLE, default_sender=SENDER)
    )
    sent = await mailer.send(basic(subject="Printed"))
    printed = capsys.readouterr().out
    assert "Subject: Printed" in printed
    assert sent.message_id in printed
    assert printed.endswith("\n")
    for use in (lambda: mailer.outbox, mailer.clear_outbox):
        with pytest.raises(RuntimeError, match="memory backend"):
            use()


def test_the_mailer_repr_shows_the_backend_and_endpoint_only() -> None:
    smtp = mail.Mailer(
        EmailSettings(
            backend=EmailBackend.SMTP,
            host="smtp.example.com",
            username="mailer-user",
            password="hunter2-secret",  # type: ignore[arg-type]
            default_sender=SENDER,
        )
    )
    assert repr(smtp) == "Mailer(backend='smtp', host='smtp.example.com', port=587)"
    assert repr(memory()) == "Mailer(backend='memory')"
    assert not hasattr(smtp, "settings") and not hasattr(smtp, "_settings")


async def test_a_closed_mailer_refuses_sends_and_closing_twice_is_harmless() -> None:
    mailer = memory()
    await mailer.aclose()
    await mailer.aclose()
    error = await refused(mailer, basic())
    assert error.kind is mail.EmailErrorKind.CLOSED
    assert str(error) == "the mailer is closed"


async def test_check_is_a_no_op_for_the_capture_backends() -> None:
    await memory().check()


def test_a_single_value_where_a_tuple_is_expected_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="to=\\(\\.\\.\\.,\\)"):
        mail.Message(to="ada@example.com", subject="s", text="t")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="attachments"):
        mail.Message(to=(), subject="s", attachments=mail.Attachment("a", b""))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a tuple"):
        mail.Message(to=5, subject="s")  # type: ignore[arg-type]
    listed = mail.Message(to=["a@example.com"], subject="s", text="t")  # type: ignore[arg-type]
    assert listed.to == ("a@example.com",)


def test_a_native_option_error_is_a_value_error_without_values() -> None:
    with pytest.raises(ValueError, match=r"^email: tls_ca_file") as caught:
        mail.Mailer(
            EmailSettings(backend=EmailBackend.SMTP, tls_ca_file="/nonexistent/ca.pem")
        )
    assert "nonexistent" not in str(caught.value)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"default_sender": "not an address"}, "default_sender"),
        ({"message_id_domain": "bad domain!"}, "message_id_domain"),
        ({"message_id_domain": "foo:bar[]"}, "message_id_domain"),
        ({"helo_name": "foo:bar[]"}, "helo_name"),
        ({"helo_name": "[::1]"}, "helo_name"),
    ],
)
def test_settings_refuse_a_bad_sender_or_domain_without_its_value(
    options: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        EmailSettings(backend=EmailBackend.MEMORY, **options)  # type: ignore[arg-type]
    assert str(next(iter(options.values()))) not in str(caught.value)


async def test_an_internationalized_default_sender_starts_and_uses_punycode() -> None:
    mailer = memory(default_sender="info@bücher.example")
    sent = await mailer.send(basic())
    assert sent.message_id.endswith("@xn--bcher-kva.example>")
    assert (
        await memory(message_id_domain="Bücher.example").send(basic())
    ).message_id.endswith("@xn--bcher-kva.example>")


async def test_python_side_refusals_are_counted_and_a_closed_mailer_says_closed() -> (
    None
):
    mailer = memory(default_sender=None)
    kinds = [
        (await refused(mailer, basic())).kind,
        (await refused(mailer, basic(sender="a@example.com", headers=(("X",),)))).kind,
    ]
    assert kinds == [
        mail.EmailErrorKind.INVALID_MESSAGE,
        mail.EmailErrorKind.INVALID_HEADER,
    ]
    error = await refused(mailer, basic(sender="a@example.com", headers=(("X",),)))
    assert (error.field, error.index) == ("headers", 0)
    assert mailer.stats()["failed"] == 3
    await mailer.aclose()
    assert (await refused(mailer, basic())).kind is mail.EmailErrorKind.CLOSED
    assert mailer.stats()["failed"] == 4


async def test_oversized_content_is_refused_before_it_is_copied() -> None:
    mailer = memory(max_message_bytes=2048)
    big = bytearray(4096)
    error = await refused(
        mailer,
        basic(attachments=(mail.Attachment("a.bin", big),)),  # type: ignore[arg-type]
    )
    assert (error.kind, error.field, error.index) == (
        mail.EmailErrorKind.LIMIT_EXCEEDED,
        "attachments",
        0,
    )
    view = memoryview(big)
    error = await refused(
        mailer,
        basic(attachments=(mail.Attachment("a.bin", view),)),  # type: ignore[arg-type]
    )
    assert error.kind is mail.EmailErrorKind.LIMIT_EXCEEDED
    bodies = (({"text": "é" * 1100}, "text"), ({"html": "x" * 2049}, "html"))
    for fields, field in bodies:
        error = await refused(mailer, basic(**fields))
        assert (error.kind, error.field) == (mail.EmailErrorKind.LIMIT_EXCEEDED, field)
    assert mailer.stats() == {**mailer.stats(), "sent": 0, "failed": 4}


async def test_text_that_looks_like_an_encoded_word_and_control_characters() -> None:
    mailer = memory()
    sent = await mailer.send(basic(subject="=?utf-8?b?QQ==?="))
    assert sent.raw is not None and b"Subject: =?utf-8?b?QQ==?=" not in sent.raw
    assert parse(sent)["Subject"] == "=?utf-8?b?QQ==?="
    for fields in (
        {"subject": "bell\x07"},
        {"headers": (("X-A", "a\x7fb"),)},
        {"sender": mail.Address("ada@example.com", "Ada\x00")},
    ):
        error = await refused(mailer, basic(**fields))
        assert error.kind is mail.EmailErrorKind.INVALID_HEADER


def test_sending_without_a_mailer_says_how_to_install_one() -> None:
    with pytest.raises(RuntimeError, match="no email module is installed"):
        mail.current_mailer()


async def test_use_makes_a_mailer_current_and_restores_the_previous_one() -> None:
    first, second = memory(), memory()
    with mail.use(first):
        with mail.use(second):
            await mail.send(basic())
            assert mail.current_mailer() is second
        assert mail.current_mailer() is first
        assert second.stats()["sent"] == 1 and first.stats()["sent"] == 0
    assert mail.mailer_var.get() is None
    with pytest.raises(RuntimeError):
        await mail.send(basic())
