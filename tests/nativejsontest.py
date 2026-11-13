"""The Rust JSON, query and header functions behave like the Python they replaced."""

import json
import math
from typing import Any
from urllib.parse import parse_qs

import pytest

from hinoki import native


@pytest.mark.parametrize(
    "text",
    [
        '{"a": [1, 2.5, -0, 1e3, true, null, "é\\u00e9\\n"], "b": {"c": {}}}',
        "123456789012345678901234567890",
        "-9223372036854775809",
        "18446744073709551615",
        "1.0",
        '{"a": 1, "a": 2}',
        "[]",
        '"x"',
    ],
)
def test_decode_matches_the_standard_library(text: str) -> None:
    assert native.decode_json(text.encode()) == json.loads(text)


def test_decode_pairs_surrogates_and_reads_a_marker_looking_key_as_a_key() -> None:
    assert native.decode_json(b'"\\ud83d\\ude00"') == "\U0001f600"
    odd = b'{"$serde_json::private::Number": "1"}'
    assert native.decode_json(odd) == {"$serde_json::private::Number": "1"}
    assert native.decode_json(b" \t\r\n[ 1 , 2 ]\n") == [1, 2]


def test_decode_keeps_numeric_types() -> None:
    assert type(native.decode_json(b"1")) is int
    assert type(native.decode_json(b"1.0")) is float
    assert type(native.decode_json(b"1e2")) is float
    assert native.decode_json(b"[true, 1]") == [True, 1]
    assert type(native.decode_json(b"[true]")[0]) is bool


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"[1,]",
        b"\xff",
        b"'a'",
        b"{} x",
        b"01",
        b"1.",
        b"-",
        b'"\\ud800"',
        b'"\\ud800\\u0041"',
        b'"a\nb"',
        b'{"a" 1}',
        b"\xef\xbb\xbf{}",
    ],
)
def test_decode_rejects_invalid_documents(raw: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - any ValueError is the contract
        native.decode_json(raw)


def test_decode_limits_nesting_instead_of_overflowing() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        native.decode_json(b"[" * 100000 + b"]" * 100000)


@pytest.mark.parametrize(
    "value",
    [
        {"a": [1, 2.5, True, None, 'é\n"x"'], "b": {}},
        [],
        "plain",
        0,
        -1,
        10**30,
        1.5,
        {"nested": [[[]], {"k": []}]},
    ],
)
def test_encode_round_trips_through_the_standard_library(value: Any) -> None:
    assert json.loads(native.encode_json(value)) == value


def test_encode_is_compact_utf8_and_coerces_like_json_dumps() -> None:
    assert native.encode_json({"é": (1, 2)}) == '{"é":[1,2]}'.encode()
    for keys in ({1: "a", None: "c"}, {True: "b"}, {False: "d"}):
        assert json.loads(native.encode_json(keys)) == json.loads(json.dumps(keys))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, [math.nan]])
def test_encode_refuses_non_finite_numbers(value: Any) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        native.encode_json(value)


@pytest.mark.parametrize("value", [object(), {1, 2}, b"x", {(1, 2): 1}])
def test_encode_refuses_what_json_cannot_hold(value: Any) -> None:
    with pytest.raises(TypeError):
        native.encode_json(value)


def test_encode_limits_nesting() -> None:
    deep: Any = []
    for _ in range(5000):
        deep = [deep]
    with pytest.raises(ValueError, match="nested"):
        native.encode_json(deep)


def test_query_strings_match_parse_qs_for_valid_input() -> None:
    raw = "a=1&b&c=&a=x%20y+z&&d=%zz&e=%41"
    expected = parse_qs(raw, keep_blank_values=True, errors="strict")
    assert native.parse_query_string(raw.encode()) == expected


def test_query_strings_reject_non_ascii_and_bad_utf8() -> None:
    for raw in ("é=1".encode(), b"a=%ff", b"%ff=1"):
        with pytest.raises(ValueError):  # noqa: PT011
            native.parse_query_string(raw)


def test_header_dict_lowers_names_and_joins_repeats() -> None:
    found = native.header_dict([(b"X-A", b"1"), (b"x-b", b"\xe9"), (b"x-a", b"3")])
    assert found == {"x-a": "1, 3", "x-b": "é"}
