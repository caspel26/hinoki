"""Typed settings: loading, precedence, errors, secrets, wiring."""
# ruff: noqa: S105 - fixture values, not secrets

import json
import logging
from pathlib import Path
from typing import Literal

import pytest

from hinoki import Hinoki, Request, http
from hinoki.__main__ import main
from hinoki.settings import (
    AppSettings,
    DatabaseSettings,
    JsonFormatter,
    LoggingSettings,
    Secret,
    Settings,
    SettingsError,
    parse_dotenv,
)
from hinoki.testing import ASGIClient


class Mail(Settings):
    host: str = "localhost"
    port: int = 25
    tls: bool = False
    tags: list[str]
    mode: Literal["a", "b"] = "a"
    reply_to: str | None = None
    ratio: float = 0.5


class SampleSettings(AppSettings):
    jwt_secret: Secret
    database: DatabaseSettings
    mail: Mail


BASE = {
    "APP_JWT_SECRET": "s3cr3t-value",
    "APP_DATABASE__URL": "postgres://u:pw@h/db",
    "APP_MAIL__TAGS": "x, y",
}


def load(**extra: str) -> SampleSettings:
    return SampleSettings.load(prefix="APP_", env={**BASE, **extra})


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
    settings = SampleSettings.load(
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


def test_toml_is_typed_and_follows_dotenv_and_environment_precedence(
    tmp_path: Path,
) -> None:
    toml = tmp_path / "settings.toml"
    toml.write_text(
        '[mail]\nport = 6000\ntags = ["one", "two"]\n[execution]\nsync_workers = 4\n'
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_MAIL__PORT=5000\nAPP_MAIL__HOST=dotenv\n")
    env = {**BASE, "APP_MAIL__PORT": "587"}
    del env["APP_MAIL__TAGS"]
    settings = SampleSettings.load(
        prefix="APP_",
        env=env,
        dotenv=dotenv,
        toml=toml,
        overrides={"mail": {"port": 2525}},
    )
    assert settings.mail.port == 2525
    assert settings.mail.host == "dotenv"
    assert settings.mail.tags == ["one", "two"]
    assert settings.execution.sync_workers == 4

    rows = SampleSettings.explain(prefix="APP_", env=env, dotenv=dotenv, toml=toml)
    origins = {row.key: row.origin for row in rows}
    assert origins["APP_MAIL__PORT"] == "environment"
    assert origins["APP_MAIL__HOST"] == ".env"
    assert origins["APP_MAIL__TAGS"] == "toml"
    assert origins["APP_EXECUTION__SYNC_WORKERS"] == "toml"


def test_settings_class_and_cli_accept_the_declared_toml_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = tmp_path / "settings.toml"
    toml.write_text("[mail]\nport = 6123\n")

    class FileSettings(SampleSettings, prefix="APP_", toml=str(toml)):
        pass

    assert FileSettings.load(env=BASE).mail.port == 6123
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    assert (
        main(
            [
                "settings",
                "tests.settingstest:SampleSettings",
                "--prefix",
                "APP_",
                "--toml",
                str(toml),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "APP_MAIL__PORT" in output and "toml" in output and "6123" in output


def test_toml_errors_do_not_echo_values_or_secret_contents(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.toml"
    malformed.write_text('port = "SECRET-SENTINEL')
    with pytest.raises(SettingsError) as error:
        SampleSettings.load(prefix="APP_", env=BASE, toml=malformed)
    assert "cannot be read or parsed" in str(error.value)
    assert "SECRET-SENTINEL" not in str(error.value)

    secret = tmp_path / "secret.toml"
    secret.write_text('[database]\nurl = "SECRET-SENTINEL"\n')
    with pytest.raises(SettingsError) as error:
        SampleSettings.load(prefix="APP_", env=BASE, toml=secret)
    assert "secrets must come from environment" in str(error.value)
    assert "SECRET-SENTINEL" not in str(error.value)

    missing = tmp_path / "missing.toml"
    with pytest.raises(SettingsError, match="cannot be read or parsed"):
        SampleSettings.load(prefix="APP_", env=BASE, toml=missing)

    wrong_type = tmp_path / "wrong-type.toml"
    wrong_type.write_text('[mail]\nport = "587"\n')
    with pytest.raises(SettingsError, match="TOML value has an incompatible type"):
        SampleSettings.load(prefix="APP_", env=BASE, toml=wrong_type)


def test_toml_rejects_secrets_case_insensitively_and_tables_for_scalars(
    tmp_path: Path,
) -> None:
    class SecretSettings(Settings, prefix="APP_"):
        token: Secret

    for source in (
        'TOKEN = "SENTINEL"\n',
        'token = { value = "SENTINEL" }\n',
    ):
        toml = tmp_path / "secret.toml"
        toml.write_text(source)
        with pytest.raises(SettingsError) as raised:
            SecretSettings.load(prefix="APP_", env={}, toml=toml)
        assert "secrets must come from environment" in str(raised.value)
        assert "SENTINEL" not in str(raised.value)

    table = tmp_path / "table.toml"
    table.write_text('[mail]\nport = { value = "587" }\n')
    with pytest.raises(SettingsError, match="TOML value has an incompatible type"):
        SampleSettings.load(prefix="APP_", env=BASE, toml=table)


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
        SampleSettings.load(prefix="APP_", env=env)
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
        SampleSettings.load(prefix="APP_", env=BASE, overrides={"mail": {"port": True}})


@http.get("/ping")
async def ping(request: Request) -> dict[str, str]:
    return {"ok": "yes"}


@http.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(request.body)}


async def test_framework_settings_drive_the_app() -> None:
    settings = AppSettings.load(
        prefix="X_",
        env={
            "X_DOCS__TITLE": "Mine",
            "X_DOCS__OPENAPI_URL": "/openapi.json",
            "X_EXECUTION__MAX_BODY_BYTES": "100",
        },
    )
    app = Hinoki(settings=settings)
    app.include(ping, echo)
    assert (app.title, app.openapi_url) == ("Mine", "/openapi.json")
    assert Hinoki(settings=settings, title="Explicit").title == "Explicit"
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
    enabled = AppSettings.load(
        prefix="D_", env={"D_DOCS__OPENAPI_URL": "/o.json", "D_DOCS__DOCS_URL": "/docs"}
    )
    assert Hinoki(settings=enabled, docs_url=None).docs_url is None
    assert Hinoki(settings=enabled).docs_url == "/docs"
    # a foreign key with the prefix is only fatal in strict mode
    foreign = {"B_OTHER": "1"}
    with pytest.raises(SettingsError, match="B_OTHER: unknown setting"):
        AppSettings.load(prefix="B_", env=foreign)
    assert AppSettings.load(prefix="B_", env=foreign, strict=False)
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
            AppSettings.load(prefix="X_", env={key: value})
    # group overrides must be mappings; a Secret built in code is still a Secret
    with pytest.raises(SettingsError, match="must be a mapping"):
        SampleSettings.load(prefix="APP_", env=BASE, overrides={"database": "x"})
    assert "plain" not in repr(DatabaseSettings(url="plain"))  # type: ignore[arg-type]
    assert parse_dotenv("D=a # c\nE='x # y'") == {"D": "a", "E": "x # y"}


class Tuples(Settings):
    tags: tuple[str, ...] = ()


def test_tuple_fields_are_immutable() -> None:
    found = Tuples.load(prefix="T_", env={"T_TAGS": "a,b"})
    assert found.tags == ("a", "b")


async def test_content_length_above_the_limit_is_refused_before_reading() -> None:
    settings = AppSettings.load(prefix="Y_", env={"Y_EXECUTION__MAX_BODY_BYTES": "10"})
    app = Hinoki(settings=settings)
    app.include(echo)
    async with ASGIClient(app) as client:
        reply = await client.post("/echo", body=b"x", headers={"content-length": "11"})
        assert reply.status == 413


def test_explain_says_where_every_value_comes_from(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_MAIL__HOST=fromdot\nAPP_MAIL__PORT=1\n")
    rows = SampleSettings.explain(
        prefix="APP_",
        env={**BASE, "APP_MAIL__PORT": "2"},
        dotenv=dotenv,
        overrides={"mail": {"mode": "b"}},
    )
    origin = {r.key: r.origin for r in rows}
    shown = {r.key: r.value for r in rows}
    assert (
        origin["APP_MAIL__HOST"] == ".env" and origin["APP_MAIL__PORT"] == "environment"
    )
    assert origin["APP_MAIL__MODE"] == "override"
    assert origin["APP_EXECUTION__SYNC_WORKERS"] == "default"
    assert shown["APP_JWT_SECRET"] == "***" and "s3cr3t" not in str(rows)


def test_secret_files_fill_a_variable_and_never_echo_content(tmp_path: Path) -> None:
    secret = tmp_path / "jwt"
    secret.write_text("file-secret-value\n")
    env = {**BASE}
    del env["APP_JWT_SECRET"]
    env["APP_JWT_SECRET_FILE"] = str(secret)
    settings = SampleSettings.load(prefix="APP_", env=env)
    assert settings.jwt_secret.reveal() == "file-secret-value"  # newline stripped
    rows = SampleSettings.explain(prefix="APP_", env=env)
    assert {r.key: r.origin for r in rows}["APP_JWT_SECRET"] == "file"
    both = {**BASE, "APP_JWT_SECRET_FILE": str(secret)}
    with pytest.raises(SettingsError, match="both directly and through"):
        SampleSettings.load(prefix="APP_", env=both)
    missing = {**env, "APP_JWT_SECRET_FILE": str(tmp_path / "nope")}
    with pytest.raises(SettingsError, match="regular file") as raised:
        SampleSettings.load(prefix="APP_", env=missing)
    assert "file-secret" not in str(raised.value)
    big = tmp_path / "big"
    big.write_text("x" * 70_000)
    with pytest.raises(SettingsError, match="64 KiB"):
        SampleSettings.load(prefix="APP_", env={**env, "APP_JWT_SECRET_FILE": str(big)})
    with pytest.raises(SettingsError, match="unknown setting"):
        SampleSettings.load(prefix="APP_", env={**BASE, "APP_NOPE_FILE": str(secret)})


def test_the_env_example_lists_every_variable_with_its_type() -> None:
    text = SampleSettings.example("APP_")
    assert "APP_JWT_SECRET=\n" in text and "# secret, required" in text
    assert "APP_EXECUTION__SYNC_WORKERS=8" in text and "# integer" in text
    assert "APP_LOGGING__FORMAT=text" in text and "one of text|json" in text
    assert "APP_MAIL__TAGS=" in text and "# comma-separated text, required" in text


def test_the_settings_command_prints_origins_and_failures(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    target = "tests.settingstest:SampleSettings"
    assert main(["settings", target, "--prefix", "APP_"]) == 0
    out = capsys.readouterr().out
    assert "APP_JWT_SECRET" in out and "***" in out and "s3cr3t" not in out
    assert "environment" in out and "default" in out
    assert main(["settings", target, "--prefix", "APP_", "--example"]) == 0
    assert "APP_MAIL__PORT=25" in capsys.readouterr().out
    monkeypatch.delenv("APP_JWT_SECRET")
    assert main(["settings", target, "--prefix", "APP_"]) == 1
    error = capsys.readouterr().err
    assert "APP_JWT_SECRET: required" in error
    assert "fix: set the named variables" in error
    with pytest.raises(SystemExit):
        main(["settings", "nope:Nothing"])
