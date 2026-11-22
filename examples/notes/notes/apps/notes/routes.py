"""Five generated actions: list, retrieve, create, update and delete."""

from hinoki import Includable, crud

from .models import Note
from .schemas import NoteIn, NoteOut, NotePatch

routes: list[Includable] = [
    crud(
        Note,
        path="/notes",
        read=NoteOut,
        create=NoteIn,
        update=NotePatch,
        delete=True,
        public_writes=True,  # Notes has no sign-in on purpose; Tracker adds one
    )
]
