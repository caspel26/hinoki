"""Probe: what can a type checker verify about ORM-style model signatures?

The ``Model`` below is a typing-only prototype local to this fixture; it is
NOT the Hinoki ORM. It records the strategy findings used in docs/TYPING.md.
"""

from dataclasses import dataclass, field
from typing import Any, Self, dataclass_transform
from uuid import UUID, uuid4


@dataclass_transform(kw_only_default=True, field_specifiers=(field,))
class Model:
    def __init_subclass__(cls, table: str = "") -> None:
        dataclass(kw_only=True)(cls)

    @classmethod
    async def aget(cls, *, id: UUID) -> Self:
        raise NotImplementedError

    async def asave(self) -> Self:
        raise NotImplementedError


class Post(Model, table="posts"):
    title: str
    published: bool = False
    id: UUID = field(default_factory=uuid4)


async def checked() -> None:
    # Construction is fully checked: names, types, defaults, required fields.
    post = Post(title="Hello")
    saved: Post = await post.asave()
    fetched: Post = await Post.aget(id=saved.id)
    print(fetched.title.upper())

    Post(title=3)  # type: ignore[arg-type]
    Post()  # type: ignore[call-arg]
    Post(title="x", nope=1)  # type: ignore[call-arg]
    print(fetched.titel)  # type: ignore[attr-defined]


class Loose(Model, table="loose"):
    @classmethod
    async def acreate(cls, **fields: Any) -> Self:
        raise NotImplementedError


async def unchecked() -> None:
    # A generic ``acreate(**fields: Any)`` accepts anything: no static help.
    await Loose.acreate(whatever=object())
