"""Reusable access policies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from hinoki.auth import _EMPTY_FROZENSET, BaseAuth
from hinoki.blocking import is_coroutine_hook


@dataclass(frozen=True, slots=True)
class Access:
    """Which provider authenticates an action and which permissions it requires.

    ``Access(auth=tokens, permissions=["posts:write"])``.

    Declaring an ``Access`` does not protect anything by itself: only actions
    that name it are protected. Enforcement belongs to the request pipeline.
    """

    auth: BaseAuth[Any]
    permissions: frozenset[str] = field(default_factory=frozenset)
    _auth_fn: object = field(init=False, repr=False, compare=False)
    _auth_is_async: bool = field(init=False, repr=False, compare=False)

    def __init__(self, auth: BaseAuth[Any], permissions: Iterable[str] = ()) -> None:
        if not isinstance(auth, BaseAuth):
            raise TypeError("Access(auth=...) needs a BaseAuth provider")
        object.__setattr__(self, "auth", auth)
        object.__setattr__(self, "_auth_fn", type(auth).authenticate)
        object.__setattr__(self, "_auth_is_async", is_coroutine_hook(auth.authenticate))
        if isinstance(permissions, str | bytes):
            raise TypeError("permissions is a list of names, not one string")
        if not permissions:
            object.__setattr__(self, "permissions", _EMPTY_FROZENSET)
            return
        object.__setattr__(self, "permissions", frozenset(str(p) for p in permissions))
