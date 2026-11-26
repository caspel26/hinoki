"""A computed field whose return annotation names a class that does not resolve
is refused where the class is compiled, never written whole."""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hinoki import Hinoki, core
from hinoki.errors import RegistrationError
from hinoki.routing import RouteGroup
from hinoki.testing import ASGIClient

SECRET = "s3cret-forward"  # noqa: S105 - a marker, not a credential

MODELS = """
from hinoki import Schema
class Public(Schema):
    id: int
class Private(Public):
    secret: str
"""

CARD = """
from __future__ import annotations
from typing import TYPE_CHECKING
from hinoki import Schema, computed_field
if TYPE_CHECKING:
    from fwd_models import Public

class Card(Schema):
    id: int
    @computed_field
    def owner(self) -> Public:
        from fwd_models import Private
        return Private(id=1, secret="s3cret-forward")
"""


@pytest.fixture
def package(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "fwd_models.py").write_text(textwrap.dedent(MODELS))
    (tmp_path / "fwd_card.py").write_text(textwrap.dedent(CARD))
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for name in ("fwd_models", "fwd_card"):
        sys.modules.pop(name, None)


def card_class() -> Any:
    return importlib.import_module("fwd_card").Card


def test_an_unresolved_return_name_is_refused_when_the_class_is_compiled(
    package: Path,
) -> None:
    with pytest.raises(RegistrationError, match=r"Card\.owner.*Public"):
        core.encode_json(card_class()(id=1), None, False, None)


async def test_a_request_never_writes_the_value_whole(package: Path) -> None:
    card = card_class()
    group = RouteGroup("")

    async def serve() -> object:
        return card(id=1)

    serve.__annotations__ = {"return": card}
    group.get("/card")(serve)
    async with ASGIClient(Hinoki(include=[group])) as client:
        reply = await client.get("/card")
    assert reply.status == 500
    assert SECRET.encode() not in reply.raw


def test_a_name_defined_after_an_early_compile_is_resolved_then(
    package: Path,
) -> None:
    card = card_class()
    with pytest.raises(RegistrationError):
        core.encode_json(card(id=1), None, False, None)
    module: Any = importlib.import_module("fwd_card")
    module.Public = importlib.import_module("fwd_models").Public
    body = core.encode_json(card(id=1), None, False, None)
    assert SECRET.encode() not in body
    assert b'"id":1' in body.replace(b" ", b"")
