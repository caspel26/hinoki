"""A feature module that declares its CRUD through a shared helper."""

from uuid import UUID, uuid4

from hinoki import Schema
from hinoki.orm import Model, field
from tests.projects.crudhelper import readonly


class Memo(Model, table="crud_helper_memos"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str = field(max_length=40)


class MemoOut(Schema):
    id: UUID
    title: str


memos = readonly(Memo, "/memos", MemoOut)
