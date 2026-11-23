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

import os
import re
import sys
import tomllib
from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypedDict, Unpack, overload

from hinoki import core
from hinoki.errors import RegistrationError
from hinoki.fields import SecretStr
from hinoki.fieldtypes import FieldType
from hinoki.modules import Module, ModuleContext
from hinoki.schema import Schema
from hinoki.settings import EmailBackend, EmailSettings, Secret

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
    TEMPLATE = "template"


FIXED_SENTENCES = {
    EmailErrorKind.CLOSED: "the mailer is closed",
    EmailErrorKind.BUSY: "no SMTP connection available",
    EmailErrorKind.CONNECT: "could not connect to the SMTP server",
    EmailErrorKind.TIMEOUT: "SMTP operation timed out",
    EmailErrorKind.TLS: "TLS negotiation with the SMTP server failed",
    EmailErrorKind.AUTH: "SMTP authentication failed",
    EmailErrorKind.PROTOCOL: "unexpected SMTP server response",
    EmailErrorKind.TEMPLATE: "invalid template",
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

    def __init__(
        self, settings: EmailSettings, templates: Sequence[TemplateBase] = ()
    ) -> None:
        self.backend = settings.backend
        self._templates = {
            template.name: (template, template.load())
            for template in checked_templates(templates)
        }
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

    def loaded_template(self, template: TemplateBase) -> core.MailTemplate:
        """The compiled form of a template registered with this mailer (file
        templates were read when it started). ``RuntimeError`` when the template
        is not one of the ones the mailer was built with."""
        entry = self._templates.get(template.name)
        if entry is None or entry[0] is not template:
            raise RuntimeError(
                f"email template {template.name!r} is not registered: pass it to "
                "Hinoki(email_templates=[...]) or EmailModule(templates=[...])"
            )
        return entry[1]

    def _refuse(self, kind: EmailErrorKind) -> None:
        """Count a message refused before ``send`` as a failed send."""
        self._native.refuse(kind.value)

    def __repr__(self) -> str:
        return f"Mailer(backend={self.backend.value!r}{self._endpoint})"


class EmailModule(Module):
    """Opens a ``Mailer`` in each worker's lifespan and provides it
    (``ctx.get(Mailer)``); installed automatically by an ``email:
    EmailSettings`` field. With ``check_on_startup`` the lifespan connects first
    and a failure aborts the start."""

    name = "email"

    def __init__(
        self, settings: EmailSettings, templates: Sequence[TemplateBase] = ()
    ) -> None:
        self.settings = settings
        self._templates = checked_templates(templates)
        self._mailer: Mailer | None = None

    @classmethod
    def from_settings(
        cls, settings: object, templates: Sequence[TemplateBase] = ()
    ) -> EmailModule:
        group = getattr(settings, "email", None)
        if not isinstance(group, EmailSettings):
            raise TypeError(
                "EmailModule needs an `email: EmailSettings` field in the "
                "application's settings"
            )
        return cls(group, templates)

    @property
    def templates(self) -> tuple[TemplateBase, ...]:
        """The templates this module registers, in the order given."""
        return self._templates

    @property
    def mailer(self) -> Mailer:
        """The mailer of the running (most recently started) lifespan; for
        tests that inspect the outbox of an application they drive."""
        if self._mailer is None:
            raise RuntimeError("the email module is not running")
        return self._mailer

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        mailer = Mailer(self.settings, self._templates)
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


MAX_TEMPLATES: Final = 256
"""Most templates one application registers."""
TEMPLATE_LIMITS: Final[dict[str, int]] = dict(core.template_limits())
"""The fixed limits of the template engine, by name (``max_file_bytes``, ...)."""
MAX_FILE_BYTES: Final = TEMPLATE_LIMITS["max_file_bytes"]
MAX_SHAPE_DEPTH: Final = TEMPLATE_LIMITS["max_context_depth"]
MAX_SHOWN_BYTES: Final = TEMPLATE_LIMITS["max_name_bytes"]
TOML_KEYS: Final = ("subject", "template", "text", "description")
TOML_POSITION: Final = re.compile(r"\(at line (\d+), column (\d+)\)")
REFUSALS: Final = {
    "secret": "secret",
    "bytes": "bytes",
    "any": "untyped",
    "dict": "mapping",
    "tuple": "tuple",
    "union": "union",
}

type Value = (
    str | int | float | bool | list[Value] | tuple[Value, ...] | dict[str, Value] | None
)
"""What a template context holds. At run time ``dict``, ``list`` and ``tuple``
are read (no other mapping or sequence), plus any ``int`` and finite ``float``."""
type Context = Mapping[str, Value]
"""The context of a template declared without a ``Schema``."""


class TemplateReason(StrEnum):
    """Why a template was refused: the names the engine reports."""

    SYNTAX = "syntax"
    UNCLOSED_TAG = "unclosed_tag"
    UNEXPECTED_TAG = "unexpected_tag"
    UNCLOSED_BLOCK = "unclosed_block"
    INVALID_PATH = "invalid_path"
    SHADOWED_NAME = "shadowed_name"
    CONTROL_CHARACTER = "control_character"
    TOO_LARGE = "too_large"
    TOO_MANY_NODES = "too_many_nodes"
    TOO_DEEP = "too_deep"
    INVALID_NAME = "invalid_name"
    UNKNOWN_VARIABLE = "unknown_variable"
    UNUSED_FIELD = "unused_field"
    NOT_TEXT = "not_text"
    NOT_A_LIST = "not_a_list"
    NOT_AN_OBJECT = "not_an_object"
    SECRET_FIELD = "secret_field"  # noqa: S105 - a reason name, not a credential
    UNSUPPORTED_FIELD = "unsupported_field"
    MISSING_VARIABLE = "missing_variable"
    HEADER_INJECTION = "header_injection"
    OUTPUT_TOO_LARGE = "output_too_large"
    TOO_MANY_ITERATIONS = "too_many_iterations"
    CONTEXT_TOO_LARGE = "context_too_large"
    UNSUPPORTED_VALUE = "unsupported_value"
    SECRET_VALUE = "secret_value"  # noqa: S105 - a reason name, not a credential
    UNREADABLE = "unreadable"
    INVALID_FILE = "invalid_file"
    UNSAFE_CONTEXT = "unsafe_context"
    UNSAFE_URL = "unsafe_url"


@dataclass(frozen=True, slots=True)
class TemplateProblem:
    """One thing wrong with a template. ``part`` is ``"subject"``, ``"text"`` or
    ``"html"``; ``line`` and ``column`` (from 1) point into that part of the
    template source; ``variable`` is a name from the template or the ``Schema``,
    never a value of the context."""

    reason: TemplateReason
    part: str | None = None
    line: int | None = None
    column: int | None = None
    variable: str | None = None


type NativeProblem = tuple[str, str | None, int | None, int | None, str | None]


class TemplateError(EmailError):
    """A template is invalid or cannot be rendered. ``kind`` is always
    ``EmailErrorKind.TEMPLATE``; ``problems`` lists what is wrong (the first is
    mirrored in ``reason``, ``field`` (the part), ``line``, ``column`` and
    ``variable``) and ``truncated`` tells that more were found. ``str(error)``
    names the template, the reason, the part, the position and the variable;
    never a context value, a recipient or rendered text."""

    def __init__(
        self,
        *,
        template: str,
        problems: Sequence[TemplateProblem],
        truncated: bool = False,
    ) -> None:
        if not problems:
            raise ValueError("a template error lists at least one problem")
        first = problems[0]
        super().__init__(EmailErrorKind.TEMPLATE, field=first.part)
        self.template = template
        self.problems = tuple(problems)
        self.truncated = truncated
        self.reason = first.reason
        self.line = first.line
        self.column = first.column
        self.variable = first.variable

    def _native_problems(self) -> list[NativeProblem]:
        return [
            (p.reason.value, p.part, p.line, p.column, p.variable)
            for p in self.problems
        ]

    def __str__(self) -> str:
        return core.describe_template_error(
            self.template, self._native_problems(), self.truncated
        )

    def __repr__(self) -> str:
        return f"TemplateError({self.template!r}, reason={self.reason.value!r})"

    def __reduce__(self) -> tuple[object, ...]:
        return (
            template_error,
            (self.template, self._native_problems(), self.truncated),
        )


def template_error(
    template: str, problems: Sequence[NativeProblem], truncated: bool = False
) -> TemplateError:
    """The ``TemplateError`` for the plain attributes the native engine reports."""
    return TemplateError(
        template=template,
        problems=[
            TemplateProblem(TemplateReason(reason), part, line, column, variable)
            for reason, part, line, column, variable in problems
        ],
        truncated=truncated,
    )


def shown_name(name: str) -> str:
    """A template name as it may appear in an error: printable ASCII, cut."""
    return "".join(
        char if char.isascii() and char.isprintable() and char != " " else "?"
        for char in name[:MAX_SHOWN_BYTES]
    )


def template_failure(
    template: str,
    reason: TemplateReason,
    *,
    variable: str | None = None,
    position: tuple[int, int] | None = None,
) -> TemplateError:
    line, column = position if position is not None else (None, None)
    problem = TemplateProblem(reason, None, line, column, variable)
    return TemplateError(template=shown_name(template), problems=[problem])


@dataclass(frozen=True, slots=True)
class TemplateSource:
    """The parts of a template as text, before they are compiled."""

    subject: str
    html: str
    text: str | None = None
    description: str | None = None


def toml_position(error: tomllib.TOMLDecodeError) -> tuple[int, int] | None:
    found = TOML_POSITION.search(str(error))
    return None if found is None else (int(found[1]), int(found[2]))


def toml_text(
    name: str, data: dict[str, object], key: str, *, required: bool
) -> str | None:
    value = data.get(key)
    if isinstance(value, str):
        return value
    if value is None and not required:
        return None
    raise template_failure(name, TemplateReason.INVALID_FILE, variable=key)


def parse_toml(name: str, source: str) -> TemplateSource:
    """The parts of a template written as TOML (keys ``subject``, ``template``
    (the html), ``text`` and ``description``). The error of a file that is not
    valid TOML carries its position, not the parser's wording: that can quote
    the content."""
    if len(source.encode()) > MAX_FILE_BYTES:
        raise template_failure(name, TemplateReason.TOO_LARGE)
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError as error:
        position = toml_position(error)
        raise template_failure(
            name, TemplateReason.INVALID_FILE, position=position
        ) from None
    for key in data:
        if key not in TOML_KEYS:
            raise template_failure(
                name, TemplateReason.INVALID_FILE, variable=key[:MAX_SHOWN_BYTES]
            )
    return TemplateSource(
        subject=str(toml_text(name, data, "subject", required=True)),
        html=str(toml_text(name, data, "template", required=True)),
        text=toml_text(name, data, "text", required=False),
        description=toml_text(name, data, "description", required=False),
    )


def read_template_file(name: str, path: Path) -> TemplateSource:
    """Read and parse a template file: at most ``MAX_FILE_BYTES``, UTF-8."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise template_failure(name, TemplateReason.TOO_LARGE)
        source = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise template_failure(name, TemplateReason.UNREADABLE) from None
    return parse_toml(name, source)


def nested_node(
    ftype: FieldType, template: str, where: str, depth: int
) -> core._ShapeNode | None:
    """The node of a nested schema, a list or a union; ``None`` for a leaf."""
    kind = ftype.kind
    if kind == "schema" and ftype.schema is not None:
        return schema_shape(ftype.schema, template, where, depth)
    if kind == "list" and ftype.item is not None:
        return ("l", shape_node(ftype.item, template, where, depth + 1))
    if kind == "anyof":
        return union_node(ftype, template, where, depth)
    return None


def shape_node(
    ftype: FieldType, template: str, where: str, depth: int
) -> core._ShapeNode:
    if depth > MAX_SHAPE_DEPTH:
        raise template_failure(template, TemplateReason.TOO_DEEP, variable=where)
    if ftype.allows_unset:
        return ("x", "untyped")
    nested = nested_node(ftype, template, where, depth)
    if nested is not None:
        return nested
    refusal = REFUSALS.get(ftype.kind)
    return "s" if refusal is None else ("x", refusal)


def union_node(
    ftype: FieldType, template: str, where: str, depth: int
) -> core._ShapeNode:
    """A plain union is text when every variant is; otherwise it is refused."""
    nodes = [shape_node(item, template, where, depth + 1) for item in ftype.items]
    if ("x", "secret") in nodes:
        return ("x", "secret")
    return "s" if all(node == "s" for node in nodes) else ("x", "union")


def schema_shape(
    context: type[Schema], template: str, where: str = "", depth: int = 1
) -> core._Shape:
    """What the engine checks a template against: the fields of ``context`` and
    its computed fields, by Python name (the names ``dump(by_alias=False)``
    gives); nested schemas and lists keep their structure. A schema that
    overrides ``dump()`` is refused: what it dumps is not what ``fields()``
    declares."""
    if context.dump is not Schema.dump:
        raise template_failure(
            template,
            TemplateReason.UNSUPPORTED_FIELD,
            variable=where or context.__name__,
        )
    types = {name: spec.ftype for name, spec in context.fields().items()}
    for name, computed in context.computed_fields().items():
        types[name] = computed.field_type(context)
    prefix = f"{where}." if where else ""
    return {
        name: shape_node(ftype, template, f"{prefix}{name}", depth + 1)
        for name, ftype in types.items()
    }


def schema_children(
    value: Schema, template: str, where: str
) -> list[tuple[object, str]]:
    """The fields and computed fields of ``value`` with their paths. A computed
    field that returns a secret is refused: ``dump()`` would mask it, and the
    mask would be sent as if it were the value."""
    prefix = f"{where}." if where else ""
    found: list[tuple[object, str]] = []
    for name in type(value).computed_fields():
        got = getattr(value, name)
        if isinstance(got, SecretStr | Secret):
            raise template_failure(
                template, TemplateReason.SECRET_VALUE, variable=f"{prefix}{name}"
            )
        found.append((got, f"{prefix}{name}"))
    found.extend(
        (getattr(value, name), f"{prefix}{name}") for name in type(value).fields()
    )
    return found


def refuse_computed_secrets(template: str, root: Schema) -> None:
    pending: list[tuple[object, str]] = [(root, "")]
    while pending:
        value, where = pending.pop()
        if isinstance(value, Schema):
            pending.extend(schema_children(value, template, where))
        elif isinstance(value, list):
            pending.extend((item, where) for item in value)


def compile_template(
    name: str,
    parts: TemplateSource,
    context_type: type[Schema] | None,
) -> core.MailTemplate:
    shape = None if context_type is None else schema_shape(context_type, name)
    return core.MailTemplate(name, parts.subject, parts.html, parts.text, shape)


@dataclass(frozen=True, slots=True)
class RenderedTemplate:
    """The three parts of a rendered template."""

    subject: str
    text: str
    html: str


class MessageOptions(TypedDict, total=False):
    """The other ``Message`` fields ``Template.message`` passes through."""

    sender: Recipient | None
    cc: tuple[Recipient, ...]
    bcc: tuple[Recipient, ...]
    reply_to: tuple[Recipient, ...]
    attachments: tuple[Attachment, ...]
    headers: tuple[tuple[str, str], ...]


class TemplateBase:
    """What every template is, whatever its context type: the name registries
    key on, a description, the file it is read from (if any) and the context
    ``Schema`` (if any)."""

    def __init__(
        self,
        *,
        name: str,
        description: str | None,
        context_type: type[Schema] | None,
        origin: core.MailTemplate | Path,
    ) -> None:
        self.name = name
        self.description = description
        self.context_type = context_type
        self._origin = origin

    @property
    def path(self) -> Path | None:
        """The file of a template made with ``Template.file``."""
        return self._origin if isinstance(self._origin, Path) else None

    @property
    def loaded(self) -> bool:
        """Whether the template is compiled already: true for an inline template,
        false for a file one (each worker's mailer reads it at startup)."""
        return not isinstance(self._origin, Path)

    def load(self) -> core.MailTemplate:
        """The compiled template. A file template is read, parsed, compiled and
        checked now (synchronously): at most ``MAX_FILE_BYTES`` are read."""
        origin = self._origin
        if isinstance(origin, Path):
            parts = read_template_file(self.name, origin)
            if self.description is None:
                self.description = parts.description
            return compile_template(self.name, parts, self.context_type)
        return origin

    def context_tree(self, context: object) -> object:
        """What the engine reads of ``context``: a ``Schema`` instance dumped by
        field name, or the ``dict`` of an untyped template. ``TypeError`` when
        ``context`` is not what the template was declared with."""
        schema = self.context_type
        if schema is not None:
            if not isinstance(context, schema):
                raise TypeError(f"the context must be a {schema.__name__}")
            refuse_computed_secrets(self.name, context)
            return context.dump(by_alias=False)
        if not isinstance(context, Mapping):
            raise TypeError("the context must be a mapping")
        return dict(context)

    def compiled(self) -> core.MailTemplate:
        """The compiled template: itself when inline, the one the current
        worker's mailer loaded for a file template."""
        if isinstance(self._origin, Path):
            return current_mailer().loaded_template(self)
        return self._origin


class Template[C](TemplateBase):
    """An email template: a subject, an HTML body and optionally a text body,
    written in the small template language of ``docs/email-templates.md``.

    ``context=`` names the ``Schema`` the template is written for: its fields
    are the variables, and every variable and every field is checked when the
    template is compiled. Without ``context=`` the template takes a ``dict``
    (``Context``) and only the syntax is checked until it is rendered.
    """

    @overload
    def __init__[S: Schema](
        self: Template[S],
        *,
        name: str,
        subject: str,
        html: str,
        text: str | None = None,
        context: type[S],
        description: str | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self: Template[Context],
        *,
        name: str,
        subject: str,
        html: str,
        text: str | None = None,
        context: None = None,
        description: str | None = None,
    ) -> None: ...

    def __init__(
        self,
        *,
        name: str,
        subject: str,
        html: str,
        text: str | None = None,
        context: type[Schema] | None = None,
        description: str | None = None,
    ) -> None:
        parts = TemplateSource(subject, html, text)
        super().__init__(
            name=name,
            description=description,
            context_type=context,
            origin=compile_template(name, parts, context),
        )

    @overload
    @classmethod
    def file[S: Schema](
        cls,
        path: str | os.PathLike[str],
        *,
        context: type[S],
        name: str | None = None,
    ) -> Template[S]: ...

    @overload
    @classmethod
    def file(
        cls,
        path: str | os.PathLike[str],
        *,
        context: None = None,
        name: str | None = None,
    ) -> Template[Context]: ...

    @classmethod
    def file(
        cls,
        path: str | os.PathLike[str],
        *,
        context: type[Schema] | None = None,
        name: str | None = None,
    ) -> object:
        """A template read from a TOML file when the worker starts (nothing is
        read here). ``name`` defaults to the file's stem."""
        location = Path(path)
        template = cls.__new__(cls)
        TemplateBase.__init__(
            template,
            name=name or location.stem,
            description=None,
            context_type=context,
            origin=location,
        )
        return template

    @overload
    @classmethod
    def from_toml[S: Schema](
        cls, source: str, *, name: str, context: type[S]
    ) -> Template[S]: ...

    @overload
    @classmethod
    def from_toml(
        cls, source: str, *, name: str, context: None = None
    ) -> Template[Context]: ...

    @classmethod
    def from_toml(
        cls, source: str, *, name: str, context: type[Schema] | None = None
    ) -> object:
        """A template from TOML text already in memory (a database column, say),
        compiled and checked now."""
        parts = parse_toml(name, source)
        template = cls.__new__(cls)
        TemplateBase.__init__(
            template,
            name=name,
            description=parts.description,
            context_type=context,
            origin=compile_template(name, parts, context),
        )
        return template

    def render(self, context: C) -> RenderedTemplate:
        """Render the three parts. A ``Schema`` context is dumped by field name
        (``dump(by_alias=False)``); raises ``TemplateError``, ``TypeError`` for a
        context of the wrong type."""
        tree = self.context_tree(context)
        subject, text, html = self.compiled().render(tree)
        return RenderedTemplate(subject, text, html)

    def message(
        self,
        context: C,
        *,
        to: tuple[Recipient, ...] = (),
        **options: Unpack[MessageOptions],
    ) -> Message:
        """A ``Message`` of the rendered parts, ready for ``mail.send``. A
        template that cannot be rendered counts as a failed send of the current
        mailer (outcome ``template``) and raises ``TemplateError``."""
        try:
            rendered = self.render(context)
        except TemplateError:
            mailer = mailer_var.get()
            if mailer is not None:
                mailer._refuse(EmailErrorKind.TEMPLATE)
            raise
        return Message(
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html,
            to=to,
            **options,
        )


def checked_templates(templates: Iterable[object]) -> tuple[TemplateBase, ...]:
    """The templates as a tuple, or ``RegistrationError`` for a value that is not
    a template, a repeated name or more than ``MAX_TEMPLATES``."""
    items = tuple(templates)
    if len(items) > MAX_TEMPLATES:
        raise RegistrationError(f"at most {MAX_TEMPLATES} email templates")
    names: set[str] = set()
    checked: list[TemplateBase] = []
    for item in items:
        if not isinstance(item, TemplateBase):
            raise RegistrationError("email templates are hinoki.mail.Template objects")
        if item.name in names:
            raise RegistrationError(f"duplicate email template name {item.name!r}")
        names.add(item.name)
        checked.append(item)
    return tuple(checked)


__all__ = [
    "Address",
    "Attachment",
    "Context",
    "EmailError",
    "EmailErrorKind",
    "EmailModule",
    "Mailer",
    "MailerStats",
    "Message",
    "MessageOptions",
    "Recipient",
    "RenderedTemplate",
    "SentMail",
    "Template",
    "TemplateBase",
    "TemplateError",
    "TemplateProblem",
    "TemplateReason",
    "Value",
    "current_mailer",
    "mailer_var",
    "send",
    "use",
]
