"""Public generated CRUD fixture for the Rust CRUD phase 1 orientation run."""

from typing import Annotated
from uuid import UUID, uuid4

from hinoki import API, Field, Hinoki, Schema, Unset, crud, settings
from hinoki.db import DatabaseModule
from hinoki.fields import UNSET
from hinoki.orm import Model, field
from hinoki.pagination import PageNumberPagination


class Phase1Post(Model, table="phase1_posts"):
    id: UUID = field(primary_key=True, default_factory=uuid4)
    title: str
    body: str


class Phase1PostRead(Schema):
    id: UUID
    title: str
    body: str


class Phase1PostCreate(Schema):
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1)]


class Phase1PostPatch(Schema):
    title: str | Unset = UNSET


@crud(
    Phase1Post,
    path="/crud-posts",
    read=Phase1PostRead,
    create=Phase1PostCreate,
    update=Phase1PostPatch,
    public_writes=True,
    pagination=PageNumberPagination(page_size=20, max_page_size=100),
)
class Phase1Posts(API):
    pass


class Phase1Settings(settings.AppSettings, prefix="PHASE1_"):
    database: settings.DatabaseSettings


app = Hinoki(
    settings=Phase1Settings,
    include=[Phase1Posts],
    modules=[DatabaseModule],
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
