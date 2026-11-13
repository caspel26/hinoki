"""Typed settings: loading, precedence, errors, secrets, wiring."""

import json
import logging
from pathlib import Path
from typing import Literal

import pytest

from hinoki import (
    DatabaseSettings,
    FrameworkSettings,
    HinokiAPI,
    Request,
    Secret,
    Settings,
    SettingsError,
    get,
    post,
)
from hinoki.settings import JsonFormatter, LoggingSettings, parse_dotenv
from hinoki.testing import ASGIClient


class Mail(Settings):
    host: str = "localhost"
    port: int = 25
    tls: bool = False
    tags: list[str]
    mode: Literal["a", "b"] = "a"
    reply_to: str | None = None
    ratio: float = 0.5


class AppSettings(FrameworkSettings):
    jwt_secret: Secret
    database: DatabaseSettings
    mail: Mail


BASE = {
    "APP_JWT_SECRET": "s3cr3t-value",
    "APP_DATABASE__URL": "postgres://u:pw@h/db",
    "APP_MAIL__TAGS": "x, y",
}


def load(**extra: str) -> AppSettings:
    return AppSettings.load(prefix="APP_", env={**BASE, **extra})


def test_nested_groups_defaults_and_types_load_from_the_environment() -> None:
    settings = load(
        APP_MAIL__PORT="587",
        APP_MAIL__TLS="yes",
        APP_MAIL__REPLY_TO="",
        APP_EXECUTION__SYNC_WORKERS="16",
    )
    assert settings.mail.port == 587 and settings.mail.tls is True
    assert settings.mail.tags == ["x", "y"] and settings.mail.reply_to is None
    assert settings.execution.sync_workers == 16 and settings.execution.sync_queue == 64
    assert settings.database.max_size == 10 and settings.logging.level == "info"


def test_precedence_is_overrides_then_environment_then_dotenv_then_default(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('APP_MAIL__PORT=1\nexport APP_MAIL__HOST="dot"\n# note\n')
    env = {**BASE, "APP_MAIL__PORT": "2"}
    settings = AppSettings.load(
        prefix="APP_",
        env=env,
        dotenv=dotenv,
        overrides={"mail": {"mode": "b"}, "jwt_secret": "over"},
    )
    assert (settings.mail.port, settings.mail.host, settings.mail.mode) == (
        2,
        "dot",
        "b",
    )
    assert settings.jwt_secret.reveal() == "over"
    assert parse_dotenv("A='x y'\nB=\n=c\nbad") == {"A": "x y", "B": ""}


def test_every_problem_is_reported_at_once_without_values() -> None:
    env = {
        "APP_MAIL__PORT": "not-a-number-SENTINEL",
        "APP_MAIL__MODE": "zzz",
        "APP_MAIL__TLS": "maybe",
        "APP_EXECUTION__SYNC_WORKERS": "0",
        "APP_TYPO": "1",
        "APP_JWT_SECRET": "topsecret-SENTINEL",
    }
    with pytest.raises(SettingsError) as raised:
        AppSettings.load(prefix="APP_", env=env)
    text = str(raised.value)
    for expected in (
        "APP_MAIL__PORT: must be an integer",
        "APP_MAIL__MODE: must be one of",
        "APP_MAIL__TLS: must be true or false",
        "APP_EXECUTION: sync_workers must be >= 1",
        "APP_TYPO: unknown setting",
        "APP_DATABASE__URL: required, not set",
        "APP_MAIL__TAGS: required, not set",
    ):
        assert expected in text, expected
    assert "SENTINEL" not in text


def test_secrets_never_print_and_describe_redacts_them() -> None:
    settings = load()
    assert "s3cr3t-value" not in repr(settings) + str(settings.jwt_secret)
    assert "postgres://u:pw" not in repr(settings)
    shown = json.dumps(settings.describe())
    assert "s3cr3t-value" not in shown and "pw@h" not in shown and "***" in shown
    assert settings.database.url.reveal() == "postgres://u:pw@h/db"
    with pytest.raises(AttributeError):
        settings.mail.port = 1  # type: ignore[misc]


def test_float_and_int_reject_non_numbers_and_booleans() -> None:
    for key, value in (
        ("APP_MAIL__RATIO", "nan"),
        ("APP_MAIL__RATIO", "inf"),
        ("APP_MAIL__PORT", "1.5"),
        ("APP_MAIL__PORT", ""),
    ):
        with pytest.raises(SettingsError):
            load(**{key: value})
    with pytest.raises(SettingsError):
        AppSettings.load(prefix="APP_", env=BASE, overrides={"mail": {"port": True}})


@get("/ping")
async def ping(request: Request) -> dict[str, str]:
    return {"ok": "yes"}


@post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(request.body)}


async def test_framework_settings_drive_the_app() -> None:
    settings = FrameworkSettings.load(
        prefix="X_",
        env={
            "X_DOCS__TITLE": "Mine",
            "X_DOCS__OPENAPI_URL": "/openapi.json",
            "X_EXECUTION__MAX_BODY_BYTES": "100",
        },
    )
    app = HinokiAPI(settings=settings)
    app.include(ping, echo)
    assert (app.title, app.openapi_url) == ("Mine", "/openapi.json")
    assert HinokiAPI(settings=settings, title="Explicit").title == "Explicit"
    async with ASGIClient(app) as client:
        assert (await client.get("/openapi.json")).body["info"]["title"] == "Mine"
        small = await client.post("/echo", body=b"x" * 100)
        assert small.status == 200
        big = await client.post("/echo", body=b"x" * 101)
        assert big.status == 413


def test_json_log_format_and_idempotent_apply() -> None:
    logger = logging.getLogger("hinoki")
    before = list(logger.handlers)
    try:
        for _ in range(2):
            LoggingSettings(level="debug", format="json").apply()
        mine = [h for h in logger.handlers if h not in before]
        assert len(mine) == 1 and isinstance(mine[0].formatter, JsonFormatter)
        record = logging.LogRecord(
            "hinoki.x", logging.WARNING, "f", 1, "hi %s", ("you",), None
        )
        entry = json.loads(JsonFormatter().format(record))
        assert (entry["level"], entry["message"]) == ("warning", "hi you")
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)


def test_review_findings_are_closed() -> None:
    # docs can be disabled from code even when the settings enable them
    enabled = FrameworkSettings.load(
        prefix="D_", env={"D_DOCS__OPENAPI_URL": "/o.json", "D_DOCS__DOCS_URL": "/docs"}
    )
    assert HinokiAPI(settings=enabled, docs_url=None).docs_url is None
    assert HinokiAPI(settings=enabled).docs_url == "/docs"
    # a foreign key with the prefix is only fatal in strict mode
    foreign = {"B_OTHER": "1"}
    with pytest.raises(SettingsError, match="B_OTHER: unknown setting"):
        FrameworkSettings.load(prefix="B_", env=foreign)
    assert FrameworkSettings.load(prefix="B_", env=foreign, strict=False)
    # strict numbers: no underscores, other scripts, hex, exponents on ints
    for bad in ("1_000", chr(0x665), "0x10", "1e3", "99999999999999999999"):
        with pytest.raises(SettingsError):
            Mail.load(prefix="M_", env={"M_PORT": bad, "M_TAGS": "a"})
    assert Mail.load(prefix="M_", env={"M_PORT": " 5 ", "M_TAGS": "a"}).port == 5
    with pytest.raises(SettingsError):
        Mail.load(prefix="M_", env={"M_RATIO": "1_0.5", "M_TAGS": "a"})
    # bounds
    for key, value in (
        ("X_EXECUTION__MAX_BODY_BYTES", str(10**12)),
        ("X_EXECUTION__SYNC_WORKERS", "100000"),
    ):
        with pytest.raises(SettingsError):
            FrameworkSettings.load(prefix="X_", env={key: value})
    # group overrides must be mappings; a Secret built in code is still a Secret
    with pytest.raises(SettingsError, match="must be a mapping"):
        AppSettings.load(prefix="APP_", env=BASE, overrides={"database": "x"})
    assert "plain" not in repr(DatabaseSettings(url="plain"))  # type: ignore[arg-type]
    assert parse_dotenv("D=a # c\nE='x # y'") == {"D": "a", "E": "x # y"}


class Tuples(Settings):
    tags: tuple[str, ...] = ()


def test_tuple_fields_are_immutable() -> None:
    found = Tuples.load(prefix="T_", env={"T_TAGS": "a,b"})
    assert found.tags == ("a", "b")


async def test_content_length_above_the_limit_is_refused_before_reading() -> None:
    settings = FrameworkSettings.load(
        prefix="Y_", env={"Y_EXECUTION__MAX_BODY_BYTES": "10"}
    )
    app = HinokiAPI(settings=settings)
    app.include(echo)
    async with ASGIClient(app) as client:
        reply = await client.post("/echo", body=b"x", headers={"content-length": "11"})
        assert reply.status == 413
