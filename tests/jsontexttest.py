"""``hinoki.jsontext``: the text helpers that replaced the standard library codec."""

import json
import logging
import tomllib
from pathlib import Path
from typing import Any

import pytest

from hinoki import jsontext
from hinoki.cli.migrations import _migration_history
from hinoki.cli.project import _app_block
from hinoki.errors import MigrationError
from hinoki.orm.migrations import latest_state
from hinoki.scaffold import git_source
from hinoki.settings import JsonFormatter

AWKWARD = 'é😀\x7f\x01\t"\\ end'


def test_dumps_is_ascii_and_pretty_prints_like_the_standard_library() -> None:
    value = {"b": ["é", "😀"], "a": {}, "c": [], "d": [1, {"x": None}]}
    got = jsontext.dumps(value, indent=2, sort_keys=True)
    assert got == json.dumps(value, indent=2, sort_keys=True)
    assert got.isascii()


def test_dumps_calls_the_default_hook_and_refuses_non_finite_numbers() -> None:
    assert jsontext.dumps({"p": Path("x")}, default=str) == '{"p":"x"}'
    with pytest.raises(ValueError, match="JSON compliant"):
        jsontext.dumps(float("nan"))


@pytest.mark.parametrize("raw", ["[1]", b"[1]", bytearray(b"[1]")])
def test_loads_takes_text_and_bytes(raw: str | bytes | bytearray) -> None:
    assert jsontext.loads(raw) == [1]


@pytest.mark.parametrize("raw", ["{", "NaN", b"\xff", "[1] x"])
def test_loads_raises_value_error(raw: str | bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        jsontext.loads(raw)


def test_toml_value_is_valid_toml_for_text_json_would_escape_badly() -> None:
    document = (
        f"a = {jsontext.toml_value(AWKWARD)}\nb = {jsontext.toml_value(['é', 'x'])}"
    )
    assert tomllib.loads(document) == {"a": AWKWARD, "b": ["é", "x"]}


def test_manifest_and_scaffold_sources_quote_non_ascii_and_control_text() -> None:
    block = _app_block(AWKWARD, models="m", migrations="g", requires=(AWKWARD,))
    app = tomllib.loads(block)["apps"][0]
    assert (app["name"], app["requires"]) == (AWKWARD, [AWKWARD])
    source = tomllib.loads(git_source("https://example.com/é😀.git@v\x7f1"))
    assert source["tool"]["uv"]["sources"]["hinoki-api"] == {
        "git": "https://example.com/é😀.git",
        "rev": "v\x7f1",
    }


def test_migration_state_round_trips_through_its_file(tmp_path: Path) -> None:
    state: Any = {
        "format_version": 1,
        "tables": {
            "é": {
                "columns": {
                    "id": {
                        "type": "bigint",
                        "nullable": False,
                        "primary_key": True,
                        "unique": False,
                        "references": None,
                    }
                },
                "unique": [],
                "indexes": [],
            }
        },
    }
    (tmp_path / "0001-init.sql").write_text("-- x\n", encoding="utf-8")
    state_file = tmp_path / "0001-init.state.json"
    state_file.write_text(jsontext.dumps(state, indent=2) + "\n", encoding="utf-8")
    assert latest_state(tmp_path) == state
    assert state_file.read_text(encoding="utf-8") == (
        json.dumps(state, indent=2) + "\n"
    )


def test_a_corrupt_migration_state_is_a_migration_error(tmp_path: Path) -> None:
    (tmp_path / "0001-init.sql").write_text("-- x\n", encoding="utf-8")
    (tmp_path / "0001-init.state.json").write_bytes(b"\xff{")
    with pytest.raises(MigrationError, match="not valid JSON"):
        _migration_history("app", tmp_path)


def test_json_formatter_keeps_a_record_with_a_lone_surrogate() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "got %s", ("a\ud800b",), None)
    line = JsonFormatter().format(record)
    assert "\n" not in line
    assert json.loads(line)["message"] == "got a\ud800b"
