"""``hinoki admin create-superuser`` and ``set-password``."""

import asyncio
import io
from pathlib import Path

import pytest

from hinoki.__main__ import main
from hinoki.admin.users import AdminAccounts, AdminSession, AdminUser, migrations_path
from hinoki.db import Database
from hinoki.orm import migrator, use_database

PASSWORD = "correct horse battery"  # noqa: S105 - a fixture, not a credential


@pytest.fixture
def url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    target = f"sqlite://{tmp_path / 'cli.sqlite3'}"
    monkeypatch.setenv("HINOKI_DATABASE_URL", target)
    monkeypatch.chdir(tmp_path)  # no hinoki.toml here: the URL comes from the variable

    async def prepare() -> None:
        database = await Database.connect(target, max_size=1)
        await migrator.apply(database, migrations_path(), namespace="admin")
        database.close()

    asyncio.run(prepare())
    return target


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def stored(url: str) -> list[tuple[str, bool, int]]:
    async def read() -> list[tuple[str, bool, int]]:
        database = await Database.connect(url, max_size=1)
        with use_database(database):
            users = await AdminUser.all()
            sessions = await AdminSession.all().acount()
        database.close()
        return [(u.username, u.is_superuser, sessions) for u in users]

    return asyncio.run(read())


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_create_superuser_reads_the_password_from_stdin_and_never_prints_it(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, PASSWORD + "\n")

    code = main(["admin", "create-superuser", "Ada", "--password-stdin"])

    out = capsys.readouterr()
    assert code == 0 and "created superuser ada" in out.out
    assert PASSWORD not in out.out + out.err
    assert stored(url) == [("ada", True, 0)]


def test_the_password_is_asked_twice_without_echo_on_a_terminal(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(
        "sys.stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})()
    )
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))

    assert main(["admin", "create-superuser", "ada"]) == 0
    assert stored(url) == [("ada", True, 0)]


def test_two_different_answers_create_nothing(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD, PASSWORD + "x"])
    monkeypatch.setattr(
        "sys.stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})()
    )
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))

    assert main(["admin", "create-superuser", "ada"]) == 2
    assert "differ" in capsys.readouterr().err and stored(url) == []


def test_without_a_terminal_the_flag_is_required(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, PASSWORD + "\n")

    assert main(["admin", "create-superuser", "ada"]) == 2
    assert "--password-stdin" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "message"),
    [("short\n", "at least 12"), ("", "at least 12")],
)
def test_a_weak_or_missing_password_is_refused(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    stdin(monkeypatch, text)

    assert main(["admin", "create-superuser", "ada", "--password-stdin"]) == 2
    assert message in capsys.readouterr().err and stored(url) == []


def test_a_taken_username_is_refused(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, PASSWORD + "\n")
    assert main(["admin", "create-superuser", "ada", "--password-stdin"]) == 0
    stdin(monkeypatch, PASSWORD + "\n")

    assert main(["admin", "create-superuser", "ADA", "--password-stdin"]) == 2
    assert "already taken" in capsys.readouterr().err


def test_set_password_changes_it_and_ends_every_session(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, PASSWORD + "\n")
    assert main(["admin", "create-superuser", "ada", "--password-stdin"]) == 0

    async def open_session() -> None:
        database = await Database.connect(url, max_size=1)
        with use_database(database):
            user = await AdminUser.aget(username="ada")
            await AdminAccounts().open_session(user)
        database.close()

    asyncio.run(open_session())
    assert stored(url) == [("ada", True, 1)]
    stdin(monkeypatch, "a different password\n")

    code = main(["admin", "set-password", "Ada", "--password-stdin"])

    assert code == 0 and "ended their sessions" in capsys.readouterr().out
    assert stored(url) == [("ada", True, 0)]


def test_set_password_for_an_unknown_user_is_an_error(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, PASSWORD + "\n")

    assert main(["admin", "set-password", "ghost", "--password-stdin"]) == 2
    assert "no user named 'ghost'" in capsys.readouterr().err


def test_missing_admin_tables_point_to_the_registration_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HINOKI_DATABASE_URL", f"sqlite://{tmp_path / 'empty.sqlite3'}")
    monkeypatch.chdir(tmp_path)
    stdin(monkeypatch, PASSWORD + "\n")

    assert main(["admin", "create-superuser", "ada", "--password-stdin"]) == 1
    assert "hinoki app add admin" in capsys.readouterr().err
