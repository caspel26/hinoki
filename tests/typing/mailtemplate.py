"""Typed email templates: the context is the Schema the template was declared with."""

from pathlib import Path
from typing import assert_type

from hinoki import Schema, mail


class Welcome(Schema):
    name: str


TYPED = mail.Template.file(Path("welcome.toml"), context=Welcome)
UNTYPED = mail.Template(name="notice", subject="s", html="<p>x</p>")


def typed() -> None:
    assert_type(TYPED, mail.Template[Welcome])
    assert_type(UNTYPED, mail.Template[mail.Context])
    TYPED.message(Welcome(name="Ada"), to=("ada@example.com",))
    TYPED.message({"name": "Ada"})  # type: ignore[arg-type]
    TYPED.render({"name": "Ada"})  # type: ignore[arg-type]


def untyped() -> None:
    UNTYPED.message({"name": "Ada"}, to=("ada@example.com",))
    UNTYPED.message(Welcome(name="Ada"))  # type: ignore[arg-type]
