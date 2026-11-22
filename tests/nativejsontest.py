"""The Rust JSON, query and header functions behave like the Python they replaced."""

import datetime
import json
import math
import uuid
from typing import Any
from urllib.parse import parse_qs

import pytest

from hinoki import Schema, core


@pytest.mark.parametrize(
    "text",
    [
        '{"a": [1, 2.5, -0, 1e3, true, null, "é\\u00e9\\n"], "b": {"c": {}}}',
        "123456789012345678901234567890",
        "-9223372036854775809",
        "[9223372036854775807, -9223372036854775808, 9223372036854775808, -0, 0, -7]",
        "[18446744073709551616, 99999999999999999999999, 1e2, -1E-2, 0.5e+3]",
        "18446744073709551615",
        "1.0",
        '{"a": 1, "a": 2}',
        "[]",
        '"x"',
    ],
)
def test_decode_matches_the_standard_library(text: str) -> None:
    assert core.decode_json(text.encode()) == json.loads(text)


def _key_corpus() -> list[Any]:
    long_key = "k" * 33
    rows = [
        {
            "id": index,
            "name": f"n{index}",
            "": "",
            "é\u00e9": "ünï",
            "a\nb": 'q"uo\\te\u0000',
            long_key: [long_key, {long_key: None}],
            "k" * 32: index,
            "😀": "\U0001f600",
        }
        for index in range(40)
    ]
    wide = {f"key-{index}": index for index in range(500)}
    return [rows, wide, [wide, wide], {"a": {"a": {"a": ""}}}, ["", "", {"": ""}]]


@pytest.mark.parametrize("value", _key_corpus())
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_decode_matches_the_standard_library_on_repeated_and_long_keys(
    value: Any, ensure_ascii: bool
) -> None:
    text = json.dumps(value, ensure_ascii=ensure_ascii)
    assert core.decode_json(text.encode()) == json.loads(text)


def test_decode_shares_nothing_between_equal_keys_but_their_objects() -> None:
    rows = core.decode_json(
        json.dumps([{"k" + str(i % 3): i} for i in range(60)]).encode()
    )
    assert [next(iter(row)) for row in rows] == [f"k{i % 3}" for i in range(60)]
    rows[0]["extra"] = 1
    assert "extra" not in rows[1]


def test_decode_pairs_surrogates_and_reads_a_marker_looking_key_as_a_key() -> None:
    assert core.decode_json(b'"\\ud83d\\ude00"') == "\U0001f600"
    odd = b'{"$serde_json::private::Number": "1"}'
    assert core.decode_json(odd) == {"$serde_json::private::Number": "1"}
    assert core.decode_json(b" \t\r\n[ 1 , 2 ]\n") == [1, 2]


def test_decode_keeps_numeric_types() -> None:
    assert type(core.decode_json(b"1")) is int
    assert type(core.decode_json(b"1.0")) is float
    assert type(core.decode_json(b"1e2")) is float
    assert core.decode_json(b"[true, 1]") == [True, 1]
    assert type(core.decode_json(b"[true]")[0]) is bool


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
        core.decode_json(raw)


def test_decode_limits_nesting_instead_of_overflowing() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        core.decode_json(b"[" * 100000 + b"]" * 100000)


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
    assert json.loads(core.encode_json(value)) == value


def test_encode_is_compact_utf8_and_coerces_like_json_dumps() -> None:
    assert core.encode_json({"é": (1, 2)}) == '{"é":[1,2]}'.encode()
    for keys in ({1: "a", None: "c"}, {True: "b"}, {False: "d"}):
        assert json.loads(core.encode_json(keys)) == json.loads(json.dumps(keys))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, [math.nan]])
def test_encode_refuses_non_finite_numbers(value: Any) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        core.encode_json(value)


@pytest.mark.parametrize("value", [object(), {1, 2}, b"x", {(1, 2): 1}])
def test_encode_refuses_what_json_cannot_hold(value: Any) -> None:
    with pytest.raises(TypeError):
        core.encode_json(value)


def test_encode_limits_nesting() -> None:
    deep: Any = []
    for _ in range(5000):
        deep = [deep]
    with pytest.raises(ValueError, match="nested"):
        core.encode_json(deep)


@pytest.mark.parametrize("empty_innermost", [False, True], ids=["scalar", "empty"])
def test_nesting_limits_are_exact(empty_innermost: bool) -> None:
    def lists(count: int) -> Any:
        value: Any = [] if empty_innermost else 1
        for _ in range(count - 1 if empty_innermost else count):
            value = [value]
        return value

    def text(count: int) -> bytes:
        return b"[" * count + (b"" if empty_innermost else b"1") + b"]" * count

    assert json.loads(core.encode_json(lists(200))) == lists(200)
    with pytest.raises(ValueError, match="nested too deeply"):
        core.encode_json(lists(201))
    assert core.decode_json(text(128)) == lists(128)
    with pytest.raises(core.JsonDecodeError, match="nesting too deep"):
        core.decode_json(text(129))
    objects = b'{"a":' * 128 + b"{}" + b"}" * 128
    with pytest.raises(core.JsonDecodeError, match="nesting too deep"):
        core.decode_json(objects)


STDLIB_TEXT: Any = {
    "é": 'ü😀\x7f\x1f"\\\n',
    "a b": [],
    "a!": {},
    "a": [1, 2.5, None, True, {"k": []}],
    "": 3,
}


@pytest.mark.parametrize("indent", [None, 2])
def test_encode_ensure_ascii_and_sorting_match_json_dumps(indent: int | None) -> None:
    separators = (",", ":") if indent is None else None
    for sort_keys in (False, True):
        expected = json.dumps(
            STDLIB_TEXT,
            indent=indent,
            sort_keys=sort_keys,
            separators=separators,
        )
        got = core.encode_json(STDLIB_TEXT, indent, sort_keys, None, True)
        assert got == expected.encode()


def test_sorting_keys_puts_a_key_before_the_keys_it_prefixes() -> None:
    assert core.encode_json({"a!": 1, "a": 2, "a b": 3}, None, True) == (
        b'{"a":2,"a b":3,"a!":1}'
    )


def test_decode_reports_syntax_apart_from_python_refusing_a_value() -> None:
    for raw in (b"{", b"\xff", b"NaN", b'"\\ud800"'):
        with pytest.raises(core.JsonDecodeError):
            core.decode_json(raw)
    with pytest.raises(ValueError, match="limit") as refused:
        core.decode_json(b"1" * 5000)
    assert not isinstance(refused.value, core.JsonDecodeError)


def test_query_strings_match_parse_qs_for_valid_input() -> None:
    raw = "a=1&b&c=&a=x%20y+z&&d=%zz&e=%41"
    expected = parse_qs(raw, keep_blank_values=True, errors="strict")
    assert core.parse_query_string(raw.encode(), 100) == expected


def test_query_strings_reject_non_ascii_and_bad_utf8() -> None:
    for raw in ("é=1".encode(), b"a=%ff", b"%ff=1"):
        with pytest.raises(ValueError):  # noqa: PT011
            core.parse_query_string(raw, 100)


def test_header_dict_lowers_names_and_joins_repeats() -> None:
    found = core.header_dict([(b"X-A", b"1"), (b"x-b", b"\xe9"), (b"x-a", b"3")])
    assert found == {"x-a": "1, 3", "x-b": "é"}


@pytest.mark.parametrize(
    ("value", "sort_keys"),
    [
        ({"a\ud800": ["\udfff", "x\ud83d", "é"], "\ud800b": 2}, False),
        ({"b\ud800": 1, "a": 2, "\udfff": 3}, True),
    ],
)
def test_encode_writes_lone_surrogates_like_json_dumps(
    value: Any, sort_keys: bool
) -> None:
    expected = json.dumps(value, sort_keys=sort_keys, separators=(",", ":"))
    assert core.encode_json(value, None, sort_keys, None, True) == expected.encode()


def test_encode_escapes_lone_surrogates_in_utf8_output() -> None:
    raw = core.encode_json({"k\ud800": "é\udfff"})
    assert raw == b'{"k\\ud800":"\xc3\xa9\\udfff"}'


def test_sorting_orders_non_string_keys_by_their_values() -> None:
    assert core.encode_json({10: 1, 2: 1, True: 0}, None, True) == (
        json.dumps(
            {10: 1, 2: 1, True: 0}, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    with pytest.raises(TypeError):
        core.encode_json({1: 1, "a": 2}, None, True)


class _LoudInt(int):
    def __str__(self) -> str:
        return "loud"

    __repr__ = __str__


class _Color(Schema):
    name: str
    level: int


def _compact(value: Any, **options: Any) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, **options
    ).encode()


ENCODE_CORPUS: list[Any] = [
    [0, 1, -1, 9, 10, 99, 100, 12345, -12345, 2**31, -(2**31), 2**53 + 1],
    [2**63 - 1, -(2**63), 2**63, -(2**63) - 1, 10**30, -(10**30)],
    [True, False, 1, 0, None, [True], {"t": True, "i": 1}],
    {"k" * n: n for n in range(0, 40, 7)},
    {"": "", "é": "ü", "\u2028": "\u2029", 'q"b\\s': "\x00\x1f\x7f/\t\n"},
    {"emoji": "😀🎉", "cjk": "日本語", "greek": "ελληνικά", "mix": "aé😀z"},
    ["", "a", "ab" * 100, "\n" * 10],
    [[], {}, [[]], [{}], {"a": []}, {"a": {}}, ()],
    (1, (2, (3, ()))),
    {"a": {"b": {"c": [1, {"d": [2, [3, [4]]]}]}}},
    {str(i): [i, str(i), {"n": i}] for i in range(300)},
    {1: "a", None: "c", 2: "d"},
    {True: "b", False: "x"},
    "just a string",
    0,
    None,
]


@pytest.mark.parametrize("value", ENCODE_CORPUS)
@pytest.mark.parametrize("sort_keys", [False, True])
def test_encode_is_byte_identical_to_json_dumps(value: Any, sort_keys: bool) -> None:
    if (
        sort_keys
        and isinstance(value, dict)
        and any(not isinstance(key, str) for key in value)
    ):
        return
    assert core.encode_json(value, None, sort_keys) == _compact(
        value, sort_keys=sort_keys
    )


@pytest.mark.parametrize("value", ENCODE_CORPUS)
def test_encode_indent_is_byte_identical_to_json_dumps(value: Any) -> None:
    assert (
        core.encode_json(value, 2)
        == json.dumps(value, indent=2, ensure_ascii=False).encode()
    )


def test_encode_distinguishes_bool_int_and_int_subclasses() -> None:
    assert core.encode_json([True, 1, False, 0]) == b"[true,1,false,0]"
    assert core.encode_json([_LoudInt(7), 7]) == b"[7,7]"
    assert core.encode_json({_LoudInt(7): 1}) == b'{"7":1}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, b"0.0"),
        (-0.0, b"-0.0"),
        (1.0, b"1.0"),
        (100.0, b"100.0"),
        (0.1, b"0.1"),
        (1.5, b"1.5"),
        (123456789.123, b"123456789.123"),
        (1e-7, b"1e-7"),
        (1.5e-7, b"1.5e-7"),
        (-2.5e-5, b"-0.000025"),
        (1e15, b"1000000000000000.0"),
        (1e16, b"1e+16"),
        (1e21, b"1e+21"),
        (5e-324, b"5e-324"),
        (1.7976931348623157e308, b"1.7976931348623157e+308"),
    ],
)
def test_encode_formats_floats_stably(value: float, expected: bytes) -> None:
    assert core.encode_json(value) == expected
    assert json.loads(expected) == value


def test_encode_handles_list_and_dict_subclasses_and_mutation_free_reads() -> None:
    class Items(list[Any]):
        pass

    class Mapping(dict[str, Any]):
        pass

    assert core.encode_json(Items([1, [2]])) == b"[1,[2]]"
    assert core.encode_json(Mapping(a=1, b=Items([2]))) == b'{"a":1,"b":[2]}'


def test_encode_honours_an_overridden_iter_on_list_and_tuple_subclasses() -> None:
    class Items(list[Any]):
        def __iter__(self) -> Any:
            return iter([99])

    class Pair(tuple[Any, ...]):
        def __iter__(self) -> Any:
            return iter([7, 8])

    assert core.encode_json(Items([1, 2])) == b"[99]"
    assert core.encode_json(Pair((1, 2))) == b"[7,8]"
    assert (
        core.encode_json({"k": Items([1, 2])})
        == json.dumps({"k": Items([1, 2])}, separators=(",", ":")).encode()
    )


def test_encode_writes_dates_uuids_schemas_and_default_hooks() -> None:
    moment = datetime.datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=datetime.UTC)
    ident = uuid.UUID(int=12345)

    class Sub(uuid.UUID):
        pass

    value = [{"id": ident, "at": moment, "n": 1}, Sub(int=7), _Color(name="é", level=3)]
    got = json.loads(core.encode_json(value))
    assert got == [
        {"id": str(ident), "at": moment.isoformat(), "n": 1},
        str(Sub(int=7)),
        {"name": "é", "level": 3},
    ]
    assert core.encode_json({"x": ident}, default=str) == _compact({"x": str(ident)})
    assert core.encode_json({1, 2}, default=sorted) == b"[1,2]"


def test_encode_survives_a_hook_that_mutates_the_containers_being_written() -> None:
    items: list[Any] = [object(), 1]
    table: dict[str, Any] = {"a": object(), "b": 2}

    def clear_items(_: Any) -> Any:
        items.clear()

    def clear_table(_: Any) -> Any:
        table.clear()

    assert core.encode_json(items, default=clear_items) == b"[null]"
    assert core.encode_json(table, default=clear_table) == b'{"a":null,"b":2}'
