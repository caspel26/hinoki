"""The frozen public API keeps its types: WebSocket handlers, ``Field`` on a
default, ``Secret`` key material and ``FilterSet.apply``."""

from typing import Annotated, ClassVar, assert_type

from hinoki import Field, Schema, Secret, http
from hinoki.crypto import JWTKey
from hinoki.filters import FilterSet
from hinoki.orm import ManyToMany, Model, QuerySet, field, many_to_many
from hinoki.sessions import SessionSettings


@http.websocket("/rooms/{room}")
async def join(room: str, socket: http.WebSocket) -> int:
    return len(room)


class Tag(Model, table="typing_tags"):
    id: int = field(primary_key=True)


class Post(Model, table="typing_posts"):
    id: int = field(primary_key=True)
    tags: ClassVar[ManyToMany[Tag]] = many_to_many(Tag)


class Shown(Schema):
    name: str = Field(description="shown in the OpenAPI document")
    count: Annotated[int, Field(ge=0)] = 0


async def typed(
    filters: FilterSet, query: QuerySet[Post], socket: http.WebSocket
) -> None:
    assert_type(await join("lobby", socket), int)
    assert_type(filters.apply(query), QuerySet[Post])
    assert_type(Shown(name="a").name, str)
    assert_type(JWTKey(None, "HS256", Secret("k" * 32)).material(), bytes)
    assert_type(SessionSettings(csrf_secret=Secret("s" * 32)).csrf_key(), bytes)
