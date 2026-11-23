"""Email settings: environment parsing, validation, secrets, and the module the
settings install."""

import dataclasses
import logging
from pathlib import Path

import pytest

from hinoki import Hinoki, mail, settings
from hinoki.settings import (
    EmailBackend,
    EmailSecurity,
    EmailSettings,
    Secret,
    SettingsError,
)

PASSWORD = "synthetic-pw-4d7e1c"


class MailSettings(settings.AppSettings, prefix="X_"):
    email: EmailSettings


def load(**env: str) -> MailSettings:
    return MailSettings.load(env={f"X_EMAIL__{k.upper()}": v for k, v in env.items()})


def test_environment_variables_fill_the_group_and_the_password_stays_secret(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "password"
    secret_file.write_text(PASSWORD)
    loaded = MailSettings.load(
        env={
            "X_EMAIL__BACKEND": "smtp",
            "X_EMAIL__HOST": "smtp.example.com",
            "X_EMAIL__SECURITY": "tls",
            "X_EMAIL__USERNAME": "mailer",
            "X_EMAIL__PASSWORD_FILE": str(secret_file),
            "X_EMAIL__DEFAULT_SENDER": "noreply@example.com",
            "X_EMAIL__RETRIES": "3",
            "X_EMAIL__TIMEOUT": "30",
            "X_EMAIL__CHECK_ON_STARTUP": "true",
        }
    )
    group = loaded.email
    assert group.backend is EmailBackend.SMTP and group.security is EmailSecurity.TLS
    assert (group.host, group.retries, group.timeout) == ("smtp.example.com", 3, 30.0)
    assert group.check_on_startup is True
    assert group.password is not None and group.password.reveal() == PASSWORD
    assert group.resolved_port() == 465
    shown = repr(loaded) + repr(group) + str(loaded.describe())
    shown += "".join(
        f"{row.key}={row.value}"
        for row in MailSettings.explain(
            env={
                "X_EMAIL__BACKEND": "memory",
                "X_EMAIL__PASSWORD": PASSWORD,
                "X_EMAIL__USERNAME": "u",
            }
        )
    )
    assert PASSWORD not in shown
    assert "Secret('***')" in repr(group)


def test_the_backend_has_no_default() -> None:
    with pytest.raises(SettingsError, match="X_EMAIL__BACKEND: required"):
        load()
    assert "X_EMAIL__BACKEND=" in MailSettings.example()


@pytest.mark.parametrize(
    ("security", "port"),
    [("starttls", 587), ("tls", 465), ("none", 25)],
)
def test_the_port_defaults_to_the_one_of_the_security_mode(
    security: str, port: int
) -> None:
    group = load(backend="smtp", security=security, allow_insecure="true").email
    assert group.resolved_port() == port
    assert dataclasses.replace(group, port=2525).resolved_port() == 2525


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"username": "u"}, "username and password"),
        ({"password": "p"}, "username and password"),
        ({"host": ""}, "host"),
        ({"host": "a b"}, "host"),
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"max_connections": 65}, "max_connections"),
        ({"max_messages_per_connection": 0}, "max_messages_per_connection"),
        ({"retries": 6}, "retries"),
        ({"retries": -1}, "retries"),
        ({"max_recipients": 1001}, "max_recipients"),
        ({"max_message_bytes": 10}, "max_message_bytes"),
        ({"max_message_bytes": 51 * 1024 * 1024}, "max_message_bytes"),
        ({"max_attachments": 101}, "max_attachments"),
        ({"memory_capacity": 0}, "memory_capacity"),
        ({"connect_timeout": 0.0}, "connect_timeout"),
        ({"command_timeout": -1.0}, "command_timeout"),
        ({"pool_wait": float("nan")}, "pool_wait"),
        ({"data_timeout": float("inf")}, "data_timeout"),
        ({"idle_timeout": 3601.0}, "idle_timeout"),
        ({"timeout": 7200.0}, "timeout"),
        ({"backoff_max": 86400.0}, "backoff_max"),
        ({"username": "", "password": "pw"}, "username must not be empty"),
        ({"host": "a" * 254}, "host"),
        ({"host": "mail\x00.example.com"}, "host"),
        ({"timeout": 5.0, "connect_timeout": 10.0}, "timeout must be at least"),
        ({"backoff_base": 2.0, "backoff_max": 1.0}, "backoff_max"),
        ({"backend": "carrier-pigeon"}, "backend"),
        ({"security": "magic"}, "security"),
        (
            {"backend": "smtp", "security": "none", "host": "mail.example.com"},
            "allow_insecure",
        ),
    ],
)
def test_invalid_email_settings_are_refused_with_the_field_name(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        EmailSettings(**{"backend": "memory", **changes})  # type: ignore[arg-type]


def test_a_refusal_never_repeats_the_value() -> None:
    with pytest.raises(ValueError, match="security") as caught:
        EmailSettings(backend=EmailBackend.MEMORY, security="topsecret-value")  # type: ignore[arg-type]
    assert "topsecret-value" not in str(caught.value)


def test_plaintext_is_allowed_on_the_loopback_or_when_declared_insecure() -> None:
    for host in ("localhost", "127.0.0.1", "::1"):
        assert EmailSettings(backend="smtp", security="none", host=host)  # type: ignore[arg-type]
    declared = EmailSettings(
        backend=EmailBackend.SMTP,
        security="none",  # type: ignore[arg-type]
        host="mail.example.com",
        allow_insecure=True,
    )
    assert declared.security is EmailSecurity.NONE


def test_native_options_hold_the_password_only_in_the_dict_they_return() -> None:
    group = EmailSettings(
        backend=EmailBackend.SMTP,
        username="mailer",
        password=PASSWORD,  # type: ignore[arg-type]
        helo_name="mx.example.org",
    )
    assert isinstance(group.password, Secret)
    options = group.native_options()
    assert options["password"] == PASSWORD and options["port"] == 587
    assert options["helo_name"] == "mx.example.org" and "tls_ca_file" not in options
    capture = EmailSettings(backend=EmailBackend.MEMORY).native_options()
    assert "host" not in capture and "password" not in capture


def test_settings_that_declare_email_install_the_module_automatically() -> None:
    app = Hinoki(
        settings=MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    )
    modules = [m for m in app.modules if isinstance(m, mail.EmailModule)]
    assert len(modules) == 1 and modules[0].name == "email"
    assert not [m for m in Hinoki().modules if isinstance(m, mail.EmailModule)]


def test_an_explicit_module_replaces_the_automatic_one() -> None:
    own = mail.EmailModule(EmailSettings(backend=EmailBackend.CONSOLE))
    config = MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    declared = Hinoki(settings=config, modules=[own])
    assert [m for m in declared.modules if isinstance(m, mail.EmailModule)] == [own]
    installed = Hinoki(settings=config)
    installed.install(own)
    assert [m for m in installed.modules if isinstance(m, mail.EmailModule)] == [own]


def test_a_module_cannot_be_built_from_settings_without_the_group() -> None:
    with pytest.raises(TypeError, match="EmailSettings"):
        mail.EmailModule.from_settings(settings.AppSettings())
    built = mail.EmailModule.from_settings(
        MailSettings(email=EmailSettings(backend=EmailBackend.MEMORY))
    )
    assert built.settings.backend is EmailBackend.MEMORY
    with pytest.raises(RuntimeError, match="not running"):
        _ = built.mailer


def test_building_a_mailer_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        mail.Mailer(
            EmailSettings(
                backend=EmailBackend.SMTP,
                username="mailer",
                password=PASSWORD,  # type: ignore[arg-type]
            )
        )
    assert PASSWORD not in caplog.text and "mailer" not in caplog.text
