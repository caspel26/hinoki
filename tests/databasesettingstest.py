"""Database settings: a URL or an engine with separate connection fields."""

import dataclasses
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from hinoki import settings
from hinoki.db import Database, warn_unverified_tls
from hinoki.settings import (
    DatabaseEngine,
    DatabaseSettings,
    Secret,
    ServerSettings,
    SettingsError,
)

URL = os.environ.get("HINOKI_TEST_DATABASE_URL")


class AppSettings(settings.AppSettings, prefix="APP_"):
    database: settings.DatabaseSettings


def test_engine_fields_build_an_encoded_url_and_keep_the_password_secret() -> None:
    database = DatabaseSettings(
        engine=DatabaseEngine.POSTGRESQL,
        name="app",
        user="app user",
        password=Secret("p@ss/w:rd"),
        host="db.internal",
        port=5432,
        options=("sslmode=verify-full", "sslrootcert=/run/secrets/ca.pem"),
    )
    assert database.url.reveal() == (
        "postgresql://app%20user:p%40ss%2Fw%3Ard@db.internal:5432/app"
        "?sslmode=verify-full&sslrootcert=/run/secrets/ca.pem"
    )
    shown = repr(database) + json.dumps(database.describe())
    assert "p@ss" not in shown and "p%40ss" not in shown


def test_empty_password_is_omitted_from_the_built_url() -> None:
    database = DatabaseSettings(
        engine=DatabaseEngine.POSTGRESQL,
        name="app",
        user="app",
        password=Secret(""),
    )
    assert database.url.reveal() == "postgresql://app@localhost/app"


def test_server_port_maximum_is_not_reduced_by_database_validation() -> None:
    assert ServerSettings(port=65535).port == 65535
    assert (
        DatabaseSettings(
            engine=DatabaseEngine.POSTGRESQL, name="app", port=65535
        ).url.reveal()
        == "postgresql://localhost:65535/app"
    )


def test_postgresql_socket_host_is_encoded_and_non_pg_socket_is_refused() -> None:
    database = DatabaseSettings(
        engine=DatabaseEngine.POSTGRESQL,
        name="app",
        host="/var/run/postgresql",
    )
    assert database.url.reveal() == "postgresql://%2Fvar%2Frun%2Fpostgresql/app"
    with pytest.raises(ValueError, match="host must be a host name or address"):
        DatabaseSettings(engine=DatabaseEngine.MYSQL, name="app", host="/run/mysql")


@pytest.mark.parametrize("host", ["db/internal", "db?sslmode=x", "user@db", "db name"])
def test_server_hosts_reject_url_delimiters_and_whitespace(host: str) -> None:
    with pytest.raises(ValueError, match="host must be a host name or address"):
        DatabaseSettings(engine=DatabaseEngine.POSTGRESQL, name="app", host=host)


@pytest.mark.parametrize("port", [0, 65536])
def test_server_port_must_be_in_the_valid_range(port: int) -> None:
    with pytest.raises(ValueError, match="port must be between 1 and 65535"):
        DatabaseSettings(engine=DatabaseEngine.POSTGRESQL, name="app", port=port)


def test_dataclasses_replace_rebuilds_the_url_from_connection_fields() -> None:
    original = DatabaseSettings(engine=DatabaseEngine.POSTGRESQL, name="first")
    replaced = dataclasses.replace(original, name="second")
    assert replaced.url.reveal() == "postgresql://localhost/second"
    with pytest.raises(ValueError, match="either url or engine"):
        dataclasses.replace(original, url=Secret("postgresql://other/elsewhere"))


def test_non_enum_engine_is_rejected_at_construction() -> None:
    with pytest.raises(TypeError, match=r"engine must be a settings\.DatabaseEngine"):
        DatabaseSettings(engine="oracle", name="app")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"engine": DatabaseEngine.MYSQL, "name": "shop"}, "mysql://localhost/shop"),
        (
            {
                "engine": DatabaseEngine.MARIADB,
                "name": "shop",
                "host": "::1",
                "port": 3306,
            },
            "mariadb://[::1]:3306/shop",
        ),
        (
            {"engine": DatabaseEngine.SQLITE, "name": "./data.sqlite3"},
            "sqlite://./data.sqlite3",
        ),
        (
            {"engine": DatabaseEngine.SQLITE, "name": "/srv/app.db"},
            "sqlite:///srv/app.db",
        ),
        (
            {"engine": DatabaseEngine.POSTGRESQL, "name": "app", "password": "pw"},
            "postgresql://:pw@localhost/app",
        ),
        ({}, "sqlite://./.hinoki/local.sqlite3"),
    ],
)
def test_each_engine_builds_its_url(fields: dict[str, object], expected: str) -> None:
    assert DatabaseSettings(**fields).url.reveal() == expected  # type: ignore[arg-type]


def test_database_name_and_option_values_are_url_encoded() -> None:
    database = DatabaseSettings(
        engine=DatabaseEngine.POSTGRESQL,
        name="tenant/db?blue",
        options=("application_name=api,worker",),
    )
    assert database.url.reveal() == (
        "postgresql://localhost/tenant%2Fdb%3Fblue?application_name=api%2Cworker"
    )


@pytest.mark.parametrize(
    "option",
    [
        "password=plaintext-secret",
        "ssl-password=plaintext-secret",
        "access_token=plaintext-secret",
        "user=other",
        "host=other.example",
        "port=6543",
        "dbname=other",
    ],
)
def test_options_cannot_expose_credentials_or_override_connection_fields(
    option: str,
) -> None:
    with pytest.raises(
        ValueError, match="cannot contain credentials or override connection fields"
    ) as info:
        DatabaseSettings(
            engine=DatabaseEngine.POSTGRESQL,
            name="app",
            password=Secret("protected"),
            options=(option,),
        )
    assert "plaintext-secret" not in str(info.value)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"engine": DatabaseEngine.POSTGRESQL}, "engine needs name"),
        ({"name": "app", "host": "db"}, "name, host need engine"),
        (
            {
                "url": Secret("postgresql://db/app"),
                "engine": DatabaseEngine.POSTGRESQL,
                "name": "app",
            },
            "either url or engine",
        ),
        (
            {"engine": DatabaseEngine.SQLITE, "name": "a.db", "host": "db"},
            "sqlite takes only name; remove host",
        ),
        ({"engine": DatabaseEngine.SQLITE, "name": ":memory:"}, "file path"),
        (
            {
                "engine": DatabaseEngine.POSTGRESQL,
                "name": "app",
                "options": ("sslmode",),
            },
            "key=value",
        ),
    ],
)
def test_ambiguous_or_incomplete_fields_are_refused(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DatabaseSettings(**fields)  # type: ignore[arg-type]


def test_engine_fields_load_from_the_environment_and_toml(tmp_path: Path) -> None:
    env = {
        "APP_DATABASE__ENGINE": "postgresql",
        "APP_DATABASE__NAME": "app",
        "APP_DATABASE__USER": "app",
        "APP_DATABASE__PASSWORD": "from-env",
        "APP_DATABASE__HOST": "db",
    }
    loaded = AppSettings.load(env=env)
    assert loaded.database.engine is DatabaseEngine.POSTGRESQL
    assert loaded.database.url.reveal() == "postgresql://app:from-env@db/app"
    toml = tmp_path / "hinoki.toml"
    toml.write_text(
        '[database]\nengine = "mysql"\nname = "shop"\nport = 3307\n'
        'options = ["require_ssl=false"]\n'
    )
    from_file = AppSettings.load(env={}, toml=toml)
    assert (
        from_file.database.url.reveal()
        == "mysql://localhost:3307/shop?require_ssl=false"
    )
    comma_options = AppSettings.load(
        env={
            "APP_DATABASE__ENGINE": "postgresql",
            "APP_DATABASE__NAME": "app",
            "APP_DATABASE__OPTIONS": "sslmode=require,application_name=api",
        }
    )
    assert comma_options.database.options == (
        "sslmode=require",
        "application_name=api",
    )
    toml.write_text(
        '[database]\nengine = "postgresql"\nname = "app"\n'
        'options = ["application_name=api,worker"]\n'
    )
    comma_value = AppSettings.load(env={}, toml=toml)
    assert comma_value.database.url.reveal().endswith("?application_name=api%2Cworker")


def test_an_unknown_engine_names_the_choices_without_the_value() -> None:
    with pytest.raises(SettingsError) as info:
        AppSettings.load(
            env={"APP_DATABASE__ENGINE": "oracle-secret", "APP_DATABASE__NAME": "x"}
        )
    text = str(info.value)
    assert "sqlite, postgresql, mysql, mariadb" in text
    assert "oracle-secret" not in text


@pytest.mark.skipif(URL is None, reason="HINOKI_TEST_DATABASE_URL is not set")
async def test_fields_connect_to_a_real_postgresql() -> None:
    assert URL is not None
    parts = urlsplit(URL)
    database = DatabaseSettings(
        engine=DatabaseEngine.POSTGRESQL,
        name=parts.path.lstrip("/"),
        user=parts.username or "",
        password=Secret(parts.password) if parts.password else None,
        host=parts.hostname or "",
        port=parts.port,
    )
    db = await Database.connect(database.url.reveal())
    try:
        assert await db.fetch("SELECT 1 AS one") == [{"one": 1}]
    finally:
        db.close()


def test_the_password_comes_from_a_secret_file_and_never_from_toml(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "db-password"
    secret.write_text("from-file\n")
    loaded = AppSettings.load(
        env={
            "APP_DATABASE__ENGINE": "postgresql",
            "APP_DATABASE__NAME": "app",
            "APP_DATABASE__PASSWORD_FILE": str(secret),
        }
    )
    assert loaded.database.url.reveal() == "postgresql://:from-file@localhost/app"
    toml = tmp_path / "hinoki.toml"
    toml.write_text('[database]\nengine = "postgresql"\nname = "app"\npassword = "x"\n')
    with pytest.raises(SettingsError, match="secrets must come from environment"):
        AppSettings.load(env={}, toml=toml)


@pytest.mark.parametrize(
    ("url", "warns"),
    [
        ("postgresql://u:p@db.example.test/app", True),
        ("postgresql://u:p@db.example.test/app?sslmode=verify-full", False),
        ("postgresql://u:p@127.0.0.1/app", False),
        ("postgresql://u:p@localhost/app", False),
        ("postgresql://u:p@%2Fvar%2Frun%2Fpg/app", False),
        ("sqlite://./local.sqlite3", False),
    ],
)
def test_a_remote_postgres_url_without_sslmode_warns(
    url: str, warns: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="hinoki.db"):
        warn_unverified_tls(url)

    assert bool(caplog.records) is warns
    assert "u:p" not in caplog.text
