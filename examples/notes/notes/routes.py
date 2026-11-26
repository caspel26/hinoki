"""What the Notes application serves."""

from hinoki import Includable

from .apps.notes.routes import routes as notes_routes

routes: list[Includable] = [*notes_routes]
