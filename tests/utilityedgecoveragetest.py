"""Focused boundary tests for small runtime and utility modules."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import io
import json
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hinoki import UNSET, checkcli, routescli, shellcli
from hinoki.__main__ import main
from hinoki.access import Access
from hinoki.auth import BaseAuth, BearerAuth, Caller, Credentials, HeaderAuth
from hinoki.checkcli import build_rules
from hinoki.context import Handoff
from hinoki.crypto import JWTAuth, JWTKey, check_key_set
from hinoki.db import (
    ActiveTransaction,
    Database,
    DatabaseError,
    TransactionScope,
    current,
)
from hinoki.errors import InvalidBody, RegistrationError
from hinoki.management import Command
from hinoki.modules import Module, ModuleContext
from hinoki.openapicli import write_document
from hinoki.orm.naming import _resolve_display_names, _words, resolve_model_names
from hinoki.parsers import Parser
from hinoki.renderers import Renderer, TextRenderer
from hinoki.request import Headers
from hinoki.routescli import access_of
from hinoki.routing import Route
from hinoki.runtime.config import RuntimeConfig
from hinoki.runtime.listener import is_listening
from hinoki.runtime.management import modules_for_command
from hinoki.runtime.requests import decode_params, header_map, read_body
from hinoki.runtime.responses import HTTPError
from hinoki.runtime.tasks import ManagedTasks
from hinoki.waivers import WaiverError, parse_waiver


class AnonymousAuth(BaseAuth[object]):
    async def authenticate(self, credentials: Credentials) -> Caller[object] | None:
        del credentials
        return None


class HeaderTokenAuth(HeaderAuth[object]):
    async def resolve(self, value: str) -> Caller[object] | None:
        del value
        return None


class BearerTokenAuth(BearerAuth[object]):
    async def resolve_token(self, token: str) -> Caller[object] | None:
        del token
        return None


def test_small_protocol_defaults_and_unset_truth_value() -> None:
    assert not UNSET
    with pytest.raises(NotImplementedError):
        Parser().parse(b"", "")
    with pytest.raises(NotImplementedError):
        Renderer().render(None, None)  # type: ignore[arg-type]
    assert AnonymousAuth().openapi_scheme() is None
    assert AnonymousAuth().challenge is None
    assert HeaderTokenAuth().openapi_scheme() == {
        "type": "apiKey",
        "in": "header",
        "name": "authorization",
    }
    assert BearerTokenAuth().openapi_scheme() == {
        "type": "http",
        "scheme": "bearer",
    }
    assert TextRenderer().render("text", None) == b"text"  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="needs the handler"):
        TextRenderer().render(1, None)  # type: ignore[arg-type]


def test_handoff_cancels_registered_futures_and_rejects_late_work() -> None:
    handoff = Handoff()
    future: Future[object] = Future()
    assert handoff.add(future)
    handoff.close()
    assert future.cancelled()
    assert not handoff.add(Future())
    handoff.discard(future)
    assert not handoff.futures


def test_jwt_provider_rejects_a_multi_key_set_with_a_missing_kid() -> None:
    with pytest.raises(ValueError, match="every key needs a distinct kid"):
        check_key_set(
            (
                JWTKey(None, "HS256", b"s" * 32),
                JWTKey("verify", "EdDSA", b"v" * 32, True),
            )
        )

    class Tokens(JWTAuth[object]):
        keys = (JWTKey("current", "HS256", b"s" * 32),)

        async def resolve_claims(
            self, claims: dict[str, object]
        ) -> Caller[object] | None:
            del claims
            return None

    tokens = Tokens()
    assert tokens.openapi_scheme() == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    assert isinstance(tokens.now(), int)


def test_module_context_detects_a_mistyped_registry_entry() -> None:
    context = ModuleContext()
    context._provided[str] = object()
    assert context.find(str) is None
    with pytest.raises(TypeError, match="wrong type"):
        context.get(str)
    with pytest.raises(LookupError, match="was not provided"):
        context.get(int)


def test_runtime_config_rejects_nonpositive_body_and_invalid_limits() -> None:
    with pytest.raises(ValueError, match="sync_workers"):
        RuntimeConfig(sync_workers=0)
    with pytest.raises(ValueError, match="max_body_bytes"):
        RuntimeConfig(max_body_bytes=0)
    with pytest.raises(ValueError, match="max_query_bytes"):
        RuntimeConfig(max_query_bytes=0)
    with pytest.raises(ValueError, match="max_header_bytes"):
        RuntimeConfig(max_header_bytes=0)


def test_task_group_closes_coroutines_after_shutdown() -> None:
    async def work() -> None:
        await asyncio.sleep(0)

    async def exercise() -> None:
        group = ManagedTasks()
        await group.aclose()
        coroutine = work()
        with pytest.raises(RuntimeError, match="closed"):
            group.start_soon(coroutine, name="late")
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED

    asyncio.run(exercise())


def test_default_module_lifespan_is_an_empty_async_context() -> None:
    async def exercise() -> None:
        async with Module().lifespan(ModuleContext()):
            pass

    asyncio.run(exercise())


def test_command_module_resolution_deduplicates_transitive_requirements() -> None:
    class Storage(Module):
        name = "storage"

    class Service(Module):
        name = "service"
        requires = (Storage,)

    app = SimpleNamespace(modules=[Storage(), Service()])
    command = SimpleNamespace(name="inspect", requires=(Service, Storage))
    result = modules_for_command(cast(Any, app), cast(Command, command))
    assert {type(module) for module in result} == {Storage, Service}


def test_unentered_database_transaction_scope_rejects_exit() -> None:
    scope = TransactionScope(Database(object()))  # type: ignore[arg-type]

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="was not entered"):
            await scope.__aexit__(None, None, None)

    asyncio.run(exercise())


def test_nested_migration_transaction_is_rejected() -> None:
    async def exercise() -> None:
        database = Database(object())  # type: ignore[arg-type]
        parent = ActiveTransaction(database, object())  # type: ignore[arg-type]
        token = current.set(parent)
        try:
            with pytest.raises(DatabaseError, match="cannot be nested"):
                await database.migration().__aenter__()
        finally:
            current.reset(token)

    asyncio.run(exercise())


def test_request_helpers_reject_disconnected_or_malformed_input() -> None:
    async def disconnected() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def exercise() -> None:
        with pytest.raises(InvalidBody, match="disconnected"):
            await read_body(disconnected)

    asyncio.run(exercise())
    with pytest.raises(HTTPError, match="bad_path_encoding"):
        decode_params({"value": "%FF"})
    assert decode_params({"value": "%"}) == {"value": "%"}
    assert header_map({}) == {}


def test_request_headers_preserve_mapping_iteration_contract() -> None:
    headers = Headers({"X-Trace": "trace-1", "Host": "example.test"})
    assert set(headers) == {"x-trace", "host"}
    assert len(headers) == 2
    assert headers["X-TRACE"] == "trace-1"


def test_listener_treats_platform_query_failure_as_listening() -> None:
    class UnqueryableSocket:
        def getsockopt(self, *_args: int) -> int:
            raise OSError("SO_ACCEPTCONN unavailable")

    assert is_listening(UnqueryableSocket())  # type: ignore[arg-type]


def test_waiver_expiry_requires_a_date_not_a_datetime() -> None:
    with pytest.raises(WaiverError, match="date, not a time"):
        parse_waiver(
            1,
            {
                "rule": "HNK-ACC-001",
                "target": "shop.api.health",
                "reason": "intentional public health check",
                "owner": "platform",
                "expires": datetime(2027, 1, 31, tzinfo=UTC),
            },
        )


def test_openapi_writer_returns_error_for_an_unwritable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_write(*_args: object, **_kwargs: object) -> int:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "write_text", fail_write)
    assert write_document(tmp_path / "openapi.json", "{}") == 2
    assert "disk unavailable" in capsys.readouterr().err


def test_model_name_derivation_rejects_unusable_class_names() -> None:
    with pytest.raises(RegistrationError, match="cannot derive names"):
        _resolve_display_names("", [], "", "")
    with pytest.raises(RegistrationError, match="cannot derive names"):
        resolve_model_names("", "", "Explicit", "Explicit items", abstract=False)
    assert _words("some_model") == ["some", "model"]


def test_check_cli_rejects_non_rule_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkcli, "load_object", lambda _spec: object())
    with pytest.raises(TypeError, match="not a RuleSet"):
        build_rules("sample.rules", cast(Any, SimpleNamespace(rules=None)))


def test_check_cli_fails_for_an_explicitly_missing_waiver_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    waiver_path = tmp_path / "missing.toml"
    result = main(
        [
            "check",
            "tests.projects.rulesapp:create_app",
            "--rules",
            "tests.projects.rulesapp:rules",
            "--waivers",
            str(waiver_path),
        ]
    )
    assert result == 2
    assert str(waiver_path) in capsys.readouterr().err


def test_route_access_format_includes_sorted_permissions() -> None:
    route = SimpleNamespace(
        access=Access(AnonymousAuth(), permissions=("z:read", "a:write"))
    )
    assert access_of(cast(Any, route)) == "AnonymousAuth[a:write,z:read]"


def test_routes_command_lists_filters_and_reports_load_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def health() -> dict[str, str]:
        return {"status": "ok"}

    routes = [
        Route("GET", "/z", health, 200, None, False),
        Route("GET", "/a", health, 200, None, False),
    ]
    monkeypatch.setattr(
        routescli, "build_app", lambda _target: SimpleNamespace(routes=routes)
    )
    args = argparse.Namespace(target="app:app", path="/a", method="get", format="json")
    assert routescli.run_routes(args) == 0
    body = json.loads(capsys.readouterr().out)
    assert [item["path"] for item in body] == ["/a"]
    assert body[0]["access"] == "public"
    assert body[0]["handler"].endswith(".health")

    args.path = "/"
    args.method = ""
    args.format = "text"
    assert routescli.run_routes(args) == 0
    listing = capsys.readouterr().out
    assert "GET" in listing and "/a" in listing and "/z" in listing

    args.path = "/missing"
    assert routescli.run_routes(args) == 1
    assert "no routes match" in capsys.readouterr().err

    monkeypatch.setattr(routescli, "build_app", lambda _target: _raise_import_error())
    assert routescli.run_routes(args) == 2
    assert "cannot load app:app" in capsys.readouterr().err


def _raise_import_error() -> object:
    raise ImportError("module missing")


def test_shell_interrupt_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        class thread:
            @staticmethod
            def is_alive() -> bool:
                return False

        def __init__(self, _app: object) -> None:
            pass

        def start(self, _namespace: dict[str, object]) -> None:
            pass

    monkeypatch.setattr(shellcli, "ShellRuntime", Runtime)

    def interrupt_console(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(shellcli, "run_console", interrupt_console)
    output = io.StringIO()
    status = shellcli._run_session(
        cast(Any, SimpleNamespace(settings=object())),
        {},
        io.StringIO(),
        output,
        io.StringIO(),
    )
    assert status == 0
    assert output.getvalue().endswith("\n")
