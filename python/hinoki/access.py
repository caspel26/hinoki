"""Reusable access policies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Access:
    """Which provider authenticates an action and which scopes it requires.

    Declaring an ``Access`` does not protect anything by itself: only actions
    that name it are protected. Enforcement belongs to the request pipeline.
    """

    auth: object
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __init__(self, auth: object, scopes: Iterable[str] = ()) -> None:
        object.__setattr__(self, "auth", auth)
        object.__setattr__(self, "scopes", frozenset(str(s) for s in scopes))
