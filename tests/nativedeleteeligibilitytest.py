"""A DELETE route that carries a guard never takes the native fast path."""

from collections.abc import Callable
from typing import Any

import pytest

from hinoki import Access, Hinoki, RateLimitContext, Request, RouteGroup, http
from hinoki.api import API
from hinoki.auth import BaseAuth
from hinoki.runtime.pipeline import Pipeline
from hinoki.runtime.servercore import native_handler_eligible


class Nobody(BaseAuth[str]):
    async def authenticate(self, credentials: Any) -> None:
        return None


class Limit:
    async def check(self, context: RateLimitContext) -> None:
        return None


def eligible(*items: Any) -> dict[str, bool]:
    pipeline = Pipeline(Hinoki(include=list(items)))
    return {b.route.path: native_handler_eligible(b) for b in pipeline.bindings}


@http.delete("/plain/{item}", status=204)
async def plain(item: str) -> None:
    return None


@http.delete("/guarded/{item}", status=204, access=Access(auth=Nobody()))
async def guarded(item: str) -> None:
    return None


@http.delete("/limited/{item}", status=204, rate_limit=Limit())
async def limited(item: str) -> None:
    return None


@http.delete("/atomic/{item}", status=204, transaction=True)
async def atomic(item: str) -> None:
    return None


@http.delete("/request/{item}", status=204)
async def with_request(request: Request, item: str) -> None:
    return None


class Things(API, path="/things"):
    @http.delete("/{item}", status=204)
    async def remove(self, item: str) -> None:
        return None


group = RouteGroup("/group", atomic=True)


@group.delete("/{item}", status=204)
async def in_group(item: str) -> None:
    return None


def test_only_plain_and_transactional_deletes_are_native() -> None:
    found = eligible(plain, guarded, limited, atomic, with_request, Things, group)

    assert found["/plain/{item}"] is True
    native = [path for path, flag in found.items() if flag]
    assert native == [
        "/plain/{item}",
        "/guarded/{item}",
        "/limited/{item}",
        "/atomic/{item}",
        "/group/{item}",
    ]


@pytest.mark.parametrize(
    "route",
    [with_request],
    ids=["request"],
)
def test_each_guard_alone_keeps_a_delete_on_the_python_path(
    route: Callable[..., Any],
) -> None:
    assert list(eligible(route).values()) == [False]
