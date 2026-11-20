"""A shared helper that wraps ``crud(...)``, as a project library might."""

from typing import Any

from hinoki import Schema, crud
from hinoki.orm import Model
from hinoki.resources import CrudDeclaration


def readonly(
    model: type[Model], path: str, read: type[Schema], **options: Any
) -> CrudDeclaration:
    return crud(model, path=path, read=read, **options)
