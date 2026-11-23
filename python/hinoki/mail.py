"""Outbound email: declare a ``Message`` in Python, the Rust core renders and
delivers it.

    from hinoki import mail

    await mail.send(mail.Message(to=("ada@example.com",), subject="Hi", text="Hello"))

``EmailModule`` (installed by an ``email: EmailSettings`` field) opens one
``Mailer`` per worker in its lifespan and closes it at shutdown; nothing is
opened at import. There is no queue and no guaranteed delivery: ``send`` is an
``await`` that returns once the server accepted the message (or the console and
memory backends took it). Errors carry a kind and positions, never addresses,
content, server text or credentials.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from hinoki import core
from hinoki.modules import Module, ModuleContext
from hinoki.settings import EmailBackend, EmailSettings

CONSOLE_SEPARATOR = "-" * 72
HEADER_PAIR = 2


@dataclass(frozen=True, slots=True)
class Address:
    """A mailbox: a bare addr-spec and an optional display name."""

    email: str
    name: str | None = None


type Recipient = str | Address
"""A ``str`` is a bare addr-spec only: ``"Ada <ada@example.com>"`` is refused;
use ``Address("ada@example.com", "Ada")``."""


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file attached to a message; ``content_type`` is ``type/subtype``."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


def _tuple_of(name: str, value: object, single: tuple[type, ...]) -> tuple[object, ...]:
    if isinstance(value, single):
        raise TypeError(f"{name} must be a tuple, not one value: write {name}=(...,)")
    if not isinstance(value, Iterable):
        raise TypeError(f"{name} must be a tuple")
    return tuple(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """One email. ``text`` or ``html`` (or both) is required, and at least one
    recipient in ``to``, ``cc`` or ``bcc``. ``bcc`` is never written in the
    headers. ``sender`` falls back to ``EmailSettings.default_sender``.
    ``headers`` are extra ``(name, value)`` pairs; the headers Hinoki writes
    itself (From, To, Date, Message-ID, ...) are refused."""

    to: tuple[Recipient, ...] = ()
    subject: str
    text: str | None = None
    html: str | None = None
    sender: Recipient | None = None
    cc: tuple[Recipient, ...] = ()
    bcc: tuple[Recipient, ...] = ()
    reply_to: tuple[Recipient, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("to", "cc", "bcc", "reply_to"):
            value = _tuple_of(name, getattr(self, name), (str, bytes, Address))
            object.__setattr__(self, name, value)
        for name, single in (("attachments", Attachment), ("headers", str)):
            value = _tuple_of(name, getattr(self, name), (single,))
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class SentMail:
    """What ``send`` returns. ``recipients`` are the envelope addresses in their
    normalized form. ``raw`` (the rendered message) and ``message`` are kept by
    the console and memory backends only."""

    message_id: str
    recipients: tuple[str, ...]
    attempts: int
    raw: bytes | None = None
    message: Message | None = None


class EmailErrorKind(StrEnum):
    INVALID_ADDRESS = "invalid_address"
    INVALID_HEADER = "invalid_header"
    INVALID_MESSAGE = "invalid_message"
    LIMIT_EXCEEDED = "limit_exceeded"
    CLOSED = "closed"
    BUSY = "busy"
    CONNECT = "connect"
    TIMEOUT = "timeout"
    TLS = "tls"
    AUTH = "auth"
    REJECTED = "rejected"
    TEMPORARY = "temporary"
    PROTOCOL = "protocol"


FIXED_SENTENCES = {
    EmailErrorKind.CLOSED: "the mailer is closed",
    EmailErrorKind.BUSY: "no SMTP connection available",
    EmailErrorKind.CONNECT: "could not connect to the SMTP server",
    EmailErrorKind.TIMEOUT: "SMTP operation timed out",
    EmailErrorKind.TLS: "TLS negotiation with the SMTP server failed",
    EmailErrorKind.AUTH: "SMTP authentication failed",
    EmailErrorKind.PROTOCOL: "unexpected SMTP server response",
}
REPLY_SENTENCES = {
    EmailErrorKind.REJECTED: "the SMTP server rejected the message",
    EmailErrorKind.TEMPORARY: "the SMTP server deferred the message",
}


class EmailError(Exception):
    """A message was refused or could not be delivered. ``str(error)`` is a fixed
    sentence for ``kind`` plus the position or reply code: never an address, the
    subject, the body, the server's text or a credential.

    ``code`` and ``enhanced_status`` are the SMTP reply's when one came;
    ``attempts`` the delivery attempts made; ``field`` and ``index`` where a
    validation failure is (``index`` is the position in that tuple);
    ``possibly_sent`` is true only when the end of the message was written and
    no reply came, so the server may have accepted it (do not blindly resend).
    """

    def __init__(
        self,
        kind: EmailErrorKind,
        *,
        code: int | None = None,
        enhanced_status: str | None = None,
        attempts: int = 0,
        possibly_sent: bool = False,
        field: str | None = None,
        index: int | None = None,
    ) -> None:
        self.kind = kind
        self.code = code
        self.enhanced_status = enhanced_status
        self.attempts = attempts
        self.possibly_sent = possibly_sent
        self.field = field
        self.index = index
        super().__init__(kind)

    def __str__(self) -> str:
        field = self.field or "message"
        kind = self.kind
        if kind is EmailErrorKind.INVALID_ADDRESS:
            where = field if self.index is None else f"{field}[{self.index}]"
            return f"invalid email address in {where}"
        templates = {
            EmailErrorKind.INVALID_HEADER: f"invalid header value in {field}",
            EmailErrorKind.INVALID_MESSAGE: f"invalid message: {field}",
            EmailErrorKind.LIMIT_EXCEEDED: f"message exceeds the {field} limit",
        }
        if kind in templates:
            return templates[kind]
        if kind in REPLY_SENTENCES:
            sentence = REPLY_SENTENCES[kind]
            return sentence if self.code is None else f"{sentence} (code {self.code})"
        return FIXED_SENTENCES[kind]

    def __repr__(self) -> str:
        return f"EmailError({self.kind.value!r}, code={self.code!r})"

    def __reduce__(self) -> tuple[object, ...]:
        return (_restore_error, (self.kind.value, self.__dict__.copy()))


def _restore_error(kind: str, state: dict[str, object]) -> EmailError:
    state = {key: value for key, value in state.items() if key != "kind"}
    return EmailError(EmailErrorKind(kind), **state)  # type: ignore[arg-type]


class MailerStats(TypedDict):
    sent: int
    failed: int
    retries: int
    connections_open: int
    connections_idle: int


def _mailbox(value: object, field: str, index: int | None) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if (
        isinstance(value, Address)
        and isinstance(value.email, str)
        and (value.name is None or isinstance(value.name, str))
    ):
        return value.email, value.name
    raise EmailError(EmailErrorKind.INVALID_ADDRESS, field=field, index=index)


def _mailboxes(
    values: tuple[Recipient, ...], field: str
) -> list[tuple[str, str | None]]:
    return [_mailbox(value, field, index) for index, value in enumerate(values)]


def _attachment(value: object, index: int, remaining: int) -> tuple[str, str, bytes]:
    if not isinstance(value, Attachment) or not isinstance(
        value.content, bytes | bytearray | memoryview
    ):
        raise EmailError(
            EmailErrorKind.INVALID_MESSAGE, field="attachments", index=index
        )
    size = (
        value.content.nbytes
        if isinstance(value.content, memoryview)
        else len(value.content)
    )
    if size > remaining:
        raise EmailError(
            EmailErrorKind.LIMIT_EXCEEDED, field="attachments", index=index
        )
    return value.filename, value.content_type, bytes(value.content)


def _attachments(
    values: tuple[Attachment, ...], limit: int
) -> list[tuple[str, str, bytes]]:
    """The attachments as bytes; each is measured before it is copied, so a
    message over ``limit`` bytes is refused without copying it."""
    items: list[tuple[str, str, bytes]] = []
    remaining = limit
    for index, value in enumerate(values):
        item = _attachment(value, index, remaining)
        remaining -= len(item[2])
        items.append(item)
    return items


def _header(value: object, index: int) -> tuple[str, str]:
    if (
        isinstance(value, tuple)
        and len(value) == HEADER_PAIR
        and all(isinstance(part, str) for part in value)
    ):
        return value[0], value[1]
    raise EmailError(EmailErrorKind.INVALID_HEADER, field="headers", index=index)


def native_message(
    message: Message, default_sender: str | None, max_message_bytes: int
) -> core._MailMessage:
    """The plain dict the native mailer takes."""
    sender = message.sender if message.sender is not None else default_sender
    if sender is None:
        raise EmailError(EmailErrorKind.INVALID_MESSAGE, field="sender")
    return {
        "sender": _mailbox(sender, "sender", None),
        "to": _mailboxes(message.to, "to"),
        "cc": _mailboxes(message.cc, "cc"),
        "bcc": _mailboxes(message.bcc, "bcc"),
        "reply_to": _mailboxes(message.reply_to, "reply_to"),
        "subject": message.subject,
        "text": message.text,
        "html": message.html,
        "attachments": _attachments(message.attachments, max_message_bytes),
        "headers": [_header(item, index) for index, item in enumerate(message.headers)],
    }


class Mailer:
    """The email resource of one worker: it owns the connection pool of the
    ``smtp`` backend, so create it inside a running worker (``EmailModule`` does)
    and never at import. ``aclose`` refuses later sends and closes the idle
    connections; call it before the loop ends."""

    def __init__(self, settings: EmailSettings) -> None:
        self.backend = settings.backend
        self._default_sender = settings.default_sender
        self._endpoint = (
            f", host={settings.host!r}, port={settings.resolved_port()}"
            if settings.backend is EmailBackend.SMTP
            else ""
        )
        self._outbox: deque[SentMail] | None = (
            deque(maxlen=settings.memory_capacity)
            if settings.backend is EmailBackend.MEMORY
            else None
        )
        self._max_message_bytes = settings.max_message_bytes
        self._closed = False
        self._native = core.Mailer(settings.native_options())

    async def send(self, message: Message) -> SentMail:
        """Deliver ``message``; raises ``EmailError``."""
        if self._closed:
            self._native.refuse(EmailErrorKind.CLOSED.value)
            raise EmailError(EmailErrorKind.CLOSED)
        try:
            native = native_message(
                message, self._default_sender, self._max_message_bytes
            )
        except EmailError as error:
            self._native.refuse(error.kind.value)
            raise
        message_id, recipients, attempts, raw = await self._native.send(native)
        sent = SentMail(message_id, tuple(recipients), attempts, raw, None)
        if raw is None:
            return sent
        sent = SentMail(message_id, tuple(recipients), attempts, raw, message)
        if self._outbox is not None:
            self._outbox.append(sent)
        else:
            sys.stdout.write(raw.decode("ascii") + CONSOLE_SEPARATOR + "\n")
        return sent

    async def check(self) -> None:
        """``smtp``: connect, start TLS and authenticate with a new connection,
        then quit; nothing is sent. The other backends do nothing."""
        await self._native.check()

    async def aclose(self) -> None:
        """Close the mailer; safe to call twice. A later ``send`` raises
        ``EmailError`` of kind ``closed``."""
        self._closed = True
        await self._native.close()

    def stats(self) -> MailerStats:
        sent, failed, retries, open_, idle = self._native.stats()
        return {
            "sent": sent,
            "failed": failed,
            "retries": retries,
            "connections_open": open_,
            "connections_idle": idle,
        }

    @property
    def outbox(self) -> tuple[SentMail, ...]:
        """The messages the ``memory`` backend kept, oldest first (at most
        ``memory_capacity``: the oldest are dropped)."""
        if self._outbox is None:
            raise RuntimeError("only the memory backend keeps an outbox")
        return tuple(self._outbox)

    def clear_outbox(self) -> None:
        if self._outbox is None:
            raise RuntimeError("only the memory backend keeps an outbox")
        self._outbox.clear()

    def attach_metrics(self, metrics: core.MailMetrics) -> bool:
        return self._native.attach_metrics(metrics)

    def __repr__(self) -> str:
        return f"Mailer(backend={self.backend.value!r}{self._endpoint})"


class EmailModule(Module):
    """Opens a ``Mailer`` in each worker's lifespan and provides it
    (``ctx.get(Mailer)``); installed automatically by an ``email:
    EmailSettings`` field. With ``check_on_startup`` the lifespan connects first
    and a failure aborts the start."""

    name = "email"

    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings
        self._mailer: Mailer | None = None

    @classmethod
    def from_settings(cls, settings: object) -> EmailModule:
        group = getattr(settings, "email", None)
        if not isinstance(group, EmailSettings):
            raise TypeError(
                "EmailModule needs an `email: EmailSettings` field in the "
                "application's settings"
            )
        return cls(group)

    @property
    def mailer(self) -> Mailer:
        """The mailer of the running (most recently started) lifespan; for
        tests that inspect the outbox of an application they drive."""
        if self._mailer is None:
            raise RuntimeError("the email module is not running")
        return self._mailer

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        mailer = Mailer(self.settings)
        try:
            if self.settings.check_on_startup:
                await mailer.check()
            ctx.provide(Mailer, mailer)
            self._mailer = mailer
            yield
        finally:
            if self._mailer is mailer:
                self._mailer = None
            await mailer.aclose()


mailer_var: ContextVar[Mailer | None] = ContextVar("hinoki_mailer", default=None)


def current_mailer() -> Mailer:
    mailer = mailer_var.get()
    if mailer is None:
        raise RuntimeError(
            "no email module is installed: declare `email: EmailSettings` in the "
            "settings, or use hinoki.mail.use(mailer)"
        )
    return mailer


async def send(message: Message) -> SentMail:
    """Send ``message`` through the current worker's mailer."""
    return await current_mailer().send(message)


@contextmanager
def use(mailer: Mailer | None) -> Iterator[Mailer | None]:
    """Make ``mailer`` the current one inside the block (the runtime does this
    for every request, startup hook and management command); ``None`` clears it."""
    token = mailer_var.set(mailer)
    try:
        yield mailer
    finally:
        mailer_var.reset(token)


__all__ = [
    "Address",
    "Attachment",
    "EmailError",
    "EmailErrorKind",
    "EmailModule",
    "Mailer",
    "MailerStats",
    "Message",
    "Recipient",
    "SentMail",
    "current_mailer",
    "mailer_var",
    "send",
    "use",
]
