"""Post events: an audit line in the same transaction, a log line after commit.

``hinoki.toml`` registers ``handlers`` for the ``blog`` app, so nothing here
registers by import.
"""

import logging
from uuid import UUID

from hinoki import Schema, events

from .models import AuditEntry, Post

log = logging.getLogger("blog.audit")
handlers = events.Handlers("blog")


class PostPublished(Schema):
    """Emitted by ``Posts.publish``. It carries an identifier, not the row."""

    post_id: UUID


@handlers.in_transaction(events.Created[Post])
async def post_drafted(event: events.Created[Post]) -> None:
    await AuditEntry.acreate(post_id=event.instance.id, kind="drafted")


@handlers.in_transaction(PostPublished)
async def post_published(event: PostPublished) -> None:
    await AuditEntry.acreate(post_id=event.post_id, kind="published")


@handlers.after_commit(PostPublished)
async def announce(event: PostPublished) -> None:
    log.info("post %s published", event.post_id)  # never runs for a rolled-back write
