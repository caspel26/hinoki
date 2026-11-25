"""Declaring WebSocket actions: what is refused at registration."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from hinoki import Caller, Hinoki, Mount, Schema, http
from hinoki.errors import RegistrationError
from hinoki.fields import Field


async def noop(socket: http.WebSocket) -> None:
    return None


def declare(path: str = "/x", **options: Any) -> Any:
    async def handler(socket: http.WebSocket) -> None:
        return None

    return http.websocket(path, **options)(handler)


@pytest.mark.parametrize(
    ("path", "options", "message"),
    [
        ("x", {}, "starts with /"),
        ("/x?a=1", {}, "starts with /"),
        ("/x", {"message": dict}, "Schema class"),
        ("/x", {"max_message_bytes": 0}, "max_message_bytes"),
        ("/x", {"idle_timeout": 0}, "idle_timeout"),
        ("/x", {"subprotocols": "chat"}, "not one string"),
        ("/x", {"subprotocols": ["bad name"]}, "not a subprotocol name"),
        ("/x", {"subprotocols": ["chat/v1"]}, "not a subprotocol name"),
        ("/x", {"subprotocols": {"chat": "no"}}, "Access or None"),
        ("/x", {"origins": "https://a.example"}, "not one string"),
        ("/x", {"origins": ["a.example"]}, "scheme://host"),
    ],
)
def test_bad_options_are_refused(
    path: str, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        declare(path, **options)


def test_a_handler_must_be_async_and_take_a_socket() -> None:
    def blocking(socket: http.WebSocket) -> Any:
        return None

    with pytest.raises(RegistrationError, match="async def"):
        http.websocket("/x")(blocking)

    async def no_socket(room: str) -> None:
        return None

    declared = http.websocket("/x/{room}")(no_socket)
    with pytest.raises(RegistrationError, match=r"http\.WebSocket"):
        Hinoki(include=[declared])


def test_parameters_are_scalars_and_path_names_must_be_taken() -> None:
    async def listy(items: list[str], socket: http.WebSocket) -> None:
        return None

    with pytest.raises(RegistrationError, match="must be str, int"):
        Hinoki(include=[http.websocket("/x")(listy)])

    with pytest.raises(RegistrationError, match="lacks a parameter"):
        Hinoki(include=[http.websocket("/x/{room}")(noop)])


def test_a_caller_needs_an_access() -> None:
    async def anonymous(who: Caller[None], socket: http.WebSocket) -> None:
        return None

    with pytest.raises(RegistrationError, match="declares no access"):
        Hinoki(include=[http.websocket("/x")(anonymous)])


def test_a_function_declares_one_websocket_only() -> None:
    declared = declare("/a")
    with pytest.raises(RegistrationError, match="already declares"):
        http.websocket("/b")(declared)


def test_routes_are_mounted_and_looked_up_with_their_parameters() -> None:
    async def room(room: str, socket: http.WebSocket) -> None:
        return None

    app = Hinoki(include=[Mount("/chat", [http.websocket("/{room}")(room)])])
    found = app.lookup_websocket("/chat/lobby")
    assert found is not None
    assert found[1] == {"room": "lobby"}
    assert app.lookup_websocket("/chat") is None


def test_a_websocket_cannot_share_a_path_with_an_http_get_route() -> None:
    @http.get("/live")
    async def live() -> dict[str, str]:
        return {}

    app = Hinoki(include=[live])
    with pytest.raises(RegistrationError, match="also an HTTP GET route"):
        app.include(declare("/live"))

    @http.get("/same")
    async def same() -> dict[str, str]:
        return {}

    with pytest.raises(RegistrationError, match="also an HTTP GET route"):
        Hinoki(include=[declare("/same"), same])


def test_two_websockets_cannot_share_a_path() -> None:
    with pytest.raises(RegistrationError):
        Hinoki(include=[declare("/dup"), declare("/dup")])


def test_the_schema_options_accept_schema_classes() -> None:
    class Hello(Schema):
        name: str

    assert declare("/typed", message=Hello, reply=Hello) is not None


@pytest.mark.parametrize("where", ["/ws/{room}", "/ws"])
def test_field_on_a_websocket_scalar_parameter_is_refused(where: str) -> None:
    async def handler(
        socket: http.WebSocket, room: Annotated[str, Field(max_length=3)]
    ) -> None:
        return None

    with pytest.raises(RegistrationError, match=r"handler.*'room'.*not enforced"):
        Hinoki().include(http.websocket(where)(handler))


def test_a_plain_annotated_websocket_parameter_still_registers() -> None:
    async def handler(
        socket: http.WebSocket, room: Annotated[str, "doc"] = "a"
    ) -> None:
        return None

    Hinoki().include(http.websocket("/ws/{room}")(handler))
