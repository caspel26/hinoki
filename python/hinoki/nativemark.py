"""``@native``: mark an async handler as driven by the Rust server.

    from hinoki import http, native

    @http.get("/notes/{note_id}")
    @native
    async def read(note_id: int) -> NoteOut:
        return await Note.aget(note_id)

A native handler may await only Hinoki's database calls. Any other await
(``asyncio.sleep``, an HTTP client, a lock) is refused with an error, because a
coroutine the Rust server has started cannot move to an event loop. The marker
returns the function unchanged, so it composes with other decorators.
"""

from __future__ import annotations

from collections.abc import Callable

MARK = "__hinoki_native__"


def native[F: Callable[..., object]](handler: F) -> F:
    """Declare that ``handler`` awaits only native (database) awaitables."""
    setattr(handler, MARK, True)
    return handler


def is_native(handler: object) -> bool:
    """Whether ``handler`` carries the ``@native`` marker."""
    return getattr(handler, MARK, False) is True
