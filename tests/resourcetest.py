"""Worker-owned resources injected into function and class API handlers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from hinoki import API, Hinoki, Module, ModuleContext, Resource, http
from hinoki.ai.mcpapp import select
from hinoki.errors import RegistrationError
from hinoki.runtime.servercore import native_handler_eligible, route_spec
from hinoki.testing import ASGIClient


class Label(Resource):
    def __init__(self, value: str) -> None:
        self.value = value


DEFAULT_LABEL = Label("default")


class Labels(Module):
    name = "labels"

    @asynccontextmanager
    async def lifespan(self, ctx: ModuleContext) -> AsyncIterator[None]:
        ctx.provide(Label, Label("provided"))
        yield


@http.get("/async")
async def async_value(label: Label) -> dict[str, str]:
    return {"value": label.value}


@http.get("/sync")
def sync_value(label: Label) -> dict[str, str]:
    return {"value": label.value}


class LabelAPI(API, path="/class"):
    @http.get("/")
    async def value(self, label: Label) -> dict[str, str]:
        return {"value": label.value}


@http.get("/missing")
async def missing(label: Label) -> dict[str, str]:
    return {"value": label.value}


async def test_resource_is_injected_into_async_sync_and_class_handlers() -> None:
    app = Hinoki(include=[async_value, sync_value, LabelAPI], modules=[Labels()])
    async with ASGIClient(app) as client:
        for path in ("/async", "/sync", "/class"):
            response = await client.get(path)
            assert response.status == 200
            assert response.body == {"value": "provided"}


async def test_missing_resource_fails_at_startup_with_route_and_type() -> None:
    with pytest.raises(RuntimeError, match="route 'missing' requires Resource Label"):
        async with ASGIClient(Hinoki(include=[missing])):
            pytest.fail("startup must reject the missing resource provider")


def test_resource_parameter_is_not_an_openapi_or_mcp_input() -> None:
    app = Hinoki(include=[async_value])
    operation = app.openapi()["paths"]["/async"]["get"]
    assert "parameters" not in operation

    tools = select(app, ["*.async_value"], False, "/mcp")
    assert len(tools) == 1
    assert tools[0].input_schema["properties"] == {}


def test_resource_parameters_keep_the_native_handler_fast_path() -> None:
    app = Hinoki(include=[async_value])
    binding = app.bindings[0]
    assert native_handler_eligible(binding)
    spec = route_spec(0, binding, None)
    assert spec[10] == (True, [])


def test_resource_registration_rejects_invalid_shapes() -> None:
    def with_default(label: Label = DEFAULT_LABEL) -> dict[str, str]:
        return {"value": label.value}

    def path_collision(label: Label) -> dict[str, str]:
        return {"value": label.value}

    def duplicate(label: Label, other: Label) -> dict[str, str]:
        return {"value": label.value + other.value}

    cases = (
        ("/default", with_default, "cannot have a default"),
        ("/{label}", path_collision, "cannot also be a path parameter"),
        ("/duplicate", duplicate, "may be requested only once"),
    )
    for path, handler, message in cases:
        route = http.get(path)(handler)
        with pytest.raises(RegistrationError, match=message):
            Hinoki(include=[route])
