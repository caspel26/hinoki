"""Reusable access policies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from hinoki.auth import BaseAuth


@dataclass(frozen=True, slots=True)
class Access:
    """Which provider authenticates an action and which permissions it requires.

    ``Access(auth=tokens, permissions=["posts:write"])``; ``scopes=`` is the
    older spelling of the same thing and still works.

    Declaring an ``Access`` does not protect anything by itself: only actions
    that name it are protected. Enforcement belongs to the request pipeline.
    """

    auth: BaseAuth[Any]
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __init__(
        self,
        auth: BaseAuth[Any],
        scopes: Iterable[str] = (),
        *,
        permissions: Iterable[str] = (),
    ) -> None:
        if not isinstance(auth, BaseAuth):
            raise TypeError("Access(auth=...) needs a BaseAuth provider")
        given, chosen = list(scopes), list(permissions)
        if given and chosen:
            raise TypeError("use permissions=... (scopes=... is the older name)")
        object.__setattr__(self, "auth", auth)
        object.__setattr__(self, "scopes", frozenset(str(s) for s in given or chosen))

    @property
    def permissions(self) -> frozenset[str]:
        """What a caller must hold (same as ``scopes``)."""
        return self.scopes
