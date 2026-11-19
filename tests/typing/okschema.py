"""Valid schema usage: must type-check cleanly under mypy --strict."""

import datetime as dt
import enum
from collections.abc import Iterable
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from hinoki import UNSET, Field, Schema, SchemaConfig, SchemaExtra, Unset, derive
from hinoki.orm import Model, field


class TypingPost(Model):
    id: int = field(primary_key=True)
    title: str


@derive(TypingPost, only=("title",))
class TypingPostOut(Schema):
    title: str  # declared so static checkers see the derived field


def derived_out(title: str) -> TypingPostOut:
    out: TypingPostOut = TypingPostOut(title=title)
    return out


class PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=160)]
    body: str
    tags: list[str] = Field(default_factory=list)
    subtitle: str | None = None


class PostPatch(Schema):
    title: str | Unset = UNSET


class PostOut(Schema):
    id: UUID
    title: str


class Invoice(Schema):
    total: Annotated[
        Decimal, Field(ge=Decimal("0.01"), lt=10_000, max_digits=8, decimal_places=2)
    ]
    due: dt.date
    grace: dt.timedelta = dt.timedelta(days=3)
    scan: Annotated[bytes, Field(max_length=1024)] = b""


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 3


class Ticket(Schema):
    priority: Priority = Priority.LOW
    channel: Literal["web", "email"] = "web"
    labels: frozenset[str] = frozenset()
    origin: tuple[float, float] = (0.0, 0.0)


def ticket_labels() -> frozenset[str]:
    return Ticket(labels=frozenset({"a"})).labels


def ticket_origin() -> tuple[float, float]:
    return Ticket(origin=(1.0, 2.0)).origin


def ticket_priority() -> Priority:
    return Ticket(channel="email").priority


def invoice_total() -> Decimal:
    invoice = Invoice(total=Decimal("9.99"), due=dt.date(2026, 12, 1))
    return invoice.total


class VendorWebhook(Schema, config=SchemaConfig(extra=SchemaExtra.IGNORE)):
    event_id: str


def convert_many(rows: Iterable[object]) -> list[PostOut]:
    return PostOut.from_models(rows)


def build() -> str:
    data = PostCreate(title="Hello", body="text")
    patched: PostPatch = PostPatch()
    parsed: PostCreate = PostCreate.validate({"title": "x", "body": "y"})
    out = PostOut(id=UUID(int=1), title=parsed.title)
    webhook = VendorWebhook(event_id="evt-1")
    tags: list[str] = data.tags
    return data.title + out.title + webhook.event_id + str(patched.title) + str(tags)
