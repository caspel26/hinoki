"""Choosing and refusing the WSGI interface in ``hinoki serve``."""

import argparse

import pytest

from hinoki import Hinoki
from hinoki.serve import (
    load_wsgi_target,
    settings_of,
    stop_grace_of,
    wanted_interface,
    wsgi_refusal,
)
from hinoki.settings import AppSettings, ServerSettings
from hinoki.wsgi import WSGIAdapter

PLAIN = "tests.runtime.wsgiscenarios:app"
ADAPTER = "tests.runtime.wsgilimitsapp:wsgi_app"
HINOKI = "tests.kisoserverapp:app"


def args(target: str, interface: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(target=target, interface=interface)


def test_a_plain_callable_loads_and_an_adapter_too() -> None:
    assert callable(load_wsgi_target(PLAIN))
    assert isinstance(load_wsgi_target(ADAPTER), WSGIAdapter)


def test_a_hinoki_application_points_to_the_adapter() -> None:
    with pytest.raises(SystemExit, match="WSGIAdapter"):
        load_wsgi_target(HINOKI)


def test_a_missing_or_non_callable_target_is_refused() -> None:
    with pytest.raises(SystemExit, match="cannot load"):
        load_wsgi_target("tests.nope:app")
    with pytest.raises(SystemExit, match="not a WSGI callable"):
        load_wsgi_target("tests.runtime.wsgiscenarios:CHUNKS")


def test_the_flag_wins_then_the_adapter_setting_then_asgi() -> None:
    assert wanted_interface(args(HINOKI, "wsgi")) == "wsgi"
    assert wanted_interface(args(PLAIN, "asgi")) == "asgi"
    assert wanted_interface(args(HINOKI)) == "asgi"
    assert wanted_interface(args(PLAIN)) == "asgi"
    assert wanted_interface(args("tests.nope:app")) == "asgi"


def test_an_adapter_whose_settings_say_wsgi_is_served_as_wsgi() -> None:
    app = Hinoki(settings=AppSettings(server=ServerSettings(interface="wsgi")))
    adapter = WSGIAdapter(app)
    assert settings_of(adapter).interface == "wsgi"
    assert settings_of(print).interface == "asgi"  # no settings: defaults


def test_what_kiso_cannot_host_is_named() -> None:
    assert wsgi_refusal(ServerSettings()) is None
    assert "native_dispatch" in (
        wsgi_refusal(ServerSettings(native_dispatch=True)) or ""
    )
    assert "trusted_proxies" in (
        wsgi_refusal(ServerSettings(trusted_proxies=("10.0.0.0/8",))) or ""
    )


def test_a_plain_callable_gets_the_default_stop_grace() -> None:
    assert stop_grace_of(load_wsgi_target(PLAIN)) == 3 * (
        AppSettings().lifecycle.shutdown_timeout
    )
