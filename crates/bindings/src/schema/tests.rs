use super::validate::{is_canonical_uuid, is_rfc3339};

#[test]
fn canonical_uuids() {
    assert!(is_canonical_uuid("12345678-1234-5678-1234-567812345678"));
    assert!(is_canonical_uuid("ABCDEF00-1234-5678-1234-567812345678"));
    assert!(!is_canonical_uuid("12345678123456781234567812345678"));
    assert!(!is_canonical_uuid("{12345678-1234-5678-1234-567812345678}"));
    assert!(!is_canonical_uuid("1234567g-1234-5678-1234-567812345678"));
}

#[test]
fn rfc3339_shapes() {
    for good in [
        "2026-11-13T10:00:00",
        "2026-11-13t10:00:00Z",
        "2026-11-13T10:00:00.123+01:00",
        "2026-11-13T10:00:00-05:30",
    ] {
        assert!(is_rfc3339(good), "{good}");
    }
    for bad in [
        "2026-11-13",
        "2026-11-13 10:00:00",
        "2026-11-13T10:00:00.",
        "2026-11-13T10:00:00+0100",
        "2026-11-13T10:00:00Zx",
        "２０２６-11-13T10:00:00",
    ] {
        assert!(!is_rfc3339(bad), "{bad}");
    }
}

/// The Python path and the GIL-free path over the same inputs: the same
/// problems (paths, codes, messages, order) and the same accepted values, or
/// an explicit deferral by the GIL-free path.
mod differential {
    use std::cmp::Ordering;
    use std::ffi::CString;

    use pyo3::basic::CompareOp;
    use pyo3::prelude::*;
    use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
    use time::PrimitiveDateTime;

    use super::super::model::MAX_ERRORS;
    use super::super::views::{TWO_POW_63, UNCERTAIN_DATETIME};
    use super::super::*;
    use crate::request::{decode_json, decode_json_document, json_to_python, Json, JsonObject};

    const SETUP: &str = r#"
import datetime as dt
import decimal
import uuid


class Made:
    @classmethod
    def __hinoki_construct_validated__(cls, values, path):
        return (cls.__name__, values, path)


class Text(str):
    pass


def node(kind, nullable=False, item=None, index=None, **constraints):
    return (kind, nullable, constraints, item, index)
"#;

    const SCALARS: &str = r#"[(Made, [
        ("name", "name", node("str", min_length=1, max_length=5, pattern="[a-zé']+"), True),
        ("count", "count", node("int", ge=0, le=10), True),
        ("ratio", "ratio", node("float", gt=0, lt=1.5), False),
        ("flag", "flag", node("bool"), False),
        ("key", "key", node("uuid"), False),
        ("at", "at", node("datetime", nullable=True), False),
        ("note", "note", node("str", nullable=True, max_length=1), False),
        ("kind", "kind", node("str", one_of=("a", "b'c", 'é"x')), False),
        ("level", "level", node("int", one_of=(1, 2.0, True, 7)), False),
        ("yes", "yes", node("bool", one_of=(1,)), False),
        ("weight", "weight", node("float", ge=9007199254740993, one_of=(9007199254740994.0, 1e300, 3)), False),
        ("zero", "zero", node("float", one_of=(0.5, False)), False),
        ("small", "small", node("int", gt=-0.0, le=1.5), False),
        ("nan", "nan", node("float", ge=float("nan")), False),
        ("edge", "edge", node("int", ge=-9223372036854775808.0, le=9223372036854775807.0), False),
        ("wide", "wide", node("int", gt=-1e19, lt=1e19), False),
        ("digit", "digit", node("int", one_of=tuple(range(12))), False),
        ("inf", "inf", node("float", le=float("inf"), gt=-float("inf")), False),
    ], True)]"#;

    const ALIASES: &str = r#"[(Made, [
        ("user_name", "userName", node("str"), True),
        ("age", "age", node("int", nullable=True), False),
    ], True)]"#;

    const NESTED: &str = r#"[
        (Made, [
            ("items", "items", node("list", item=node("schema", index=1), min_length=1, max_length=3), True),
            ("child", "child", node("schema", nullable=True, index=1), False),
            ("tags", "tags", node("list", nullable=True, item=node("str", max_length=2)), False),
            ("grid", "grid", node("list", item=node("list", item=node("int", ge=0), max_length=2)), False),
        ], False),
        (Made, [
            ("id", "id", node("int"), True),
            ("label", "label", node("str", min_length=2), False),
        ], True),
    ]"#;

    const UNION: &str = r#"[
        (Made, [
            ("pet", "pet", node("union", discriminator="kind", discriminator_wire_name="type",
                                variants=[("cat", 1), ("dog", 2)]), True),
            ("pets", "pets", node("list", item=node("union", nullable=True, discriminator="kind",
                                  discriminator_wire_name="kind", variants=[("cat", 1), ("dog", 2)])), False),
        ], True),
        (Made, [("kind", "type", node("str"), True), ("lives", "lives", node("int", le=9), True)], True),
        (Made, [("kind", "type", node("str"), True), ("good", "good", node("bool"), False)], False),
    ]"#;

    const MAPPING: &str = r#"[
        (Made, [
            ("labels", "labels", node("dict", item=node("str", max_length=3),
                                      key=node("str", max_length=4), max_length=3), True),
            ("points", "points", node("dict", item=node("schema", index=1),
                                      key=node("str", min_length=2, max_length=256, pattern="[a-z]+")), False),
            ("grid", "grid", node("dict", nullable=True, item=node("list", nullable=True, item=node("int")),
                                  key=node("str", max_length=256), min_length=1), False),
        ], True),
        (Made, [("x", "x", node("int"), True), ("y", "y", node("int", ge=0), False)], True),
    ]"#;

    const CHAIN: &str = r#"[(Made, [
        ("next", "next", node("schema", nullable=True, index=0), False),
        ("n", "n", node("int"), False),
    ], False)]"#;

    fn compile(py: Python<'_>, definitions: &str) -> Compiled {
        let namespace = PyDict::new(py);
        py.run(&CString::new(SETUP).unwrap(), Some(&namespace), None)
            .unwrap();
        let table = py
            .eval(&CString::new(definitions).unwrap(), Some(&namespace), None)
            .unwrap();
        let uuid = py.import("uuid").unwrap().getattr("UUID").unwrap();
        let from_iso = py
            .import("datetime")
            .unwrap()
            .getattr("datetime")
            .unwrap()
            .getattr("fromisoformat")
            .unwrap();
        compile_schemas(
            py,
            table.extract().unwrap(),
            0,
            uuid.unbind(),
            from_iso.unbind(),
            py.None(),
            None,
            None,
        )
        .unwrap()
    }

    fn options(use_aliases: bool, allow_naive_datetime: bool, skip: &[&str]) -> Options {
        Options {
            construct: false,
            use_aliases,
            allow_naive_datetime,
            skip_fields: skip.iter().map(|name| (*name).to_owned()).collect(),
            input_bounds: false,
        }
    }

    /// The Python object the Python path returns for the same accepted value.
    fn python_value<'py>(py: Python<'py>, value: &Validated) -> Bound<'py, PyAny> {
        let datetime = py.import("datetime").unwrap();
        let moment = |local: PrimitiveDateTime| {
            (
                local.year(),
                u8::from(local.month()),
                local.day(),
                local.hour(),
                local.minute(),
                local.second(),
                local.microsecond(),
            )
        };
        match value {
            Validated::Null => py.None().into_bound(py),
            Validated::Bool(flag) => PyBool::new(py, *flag).to_owned().into_any(),
            Validated::Int(int) => PyInt::new(py, *int).into_any(),
            Validated::Float(float) => PyFloat::new(py, *float).into_any(),
            Validated::Str(text) => PyString::new(py, text).into_any(),
            Validated::Uuid(uuid) => py
                .import("uuid")
                .unwrap()
                .getattr("UUID")
                .unwrap()
                .call1((uuid.hyphenated().to_string(),))
                .unwrap(),
            Validated::NaiveDatetime(local) => datetime
                .getattr("datetime")
                .unwrap()
                .call1(moment(*local))
                .unwrap(),
            Validated::Datetime(aware) => {
                let delta = datetime
                    .getattr("timedelta")
                    .unwrap()
                    .call1((0, aware.offset().whole_seconds()))
                    .unwrap();
                let zone = datetime
                    .getattr("timezone")
                    .unwrap()
                    .call1((delta,))
                    .unwrap();
                let local = PrimitiveDateTime::new(aware.date(), aware.time());
                let keywords = PyDict::new(py);
                keywords.set_item("tzinfo", zone).unwrap();
                datetime
                    .getattr("datetime")
                    .unwrap()
                    .call(moment(local), Some(&keywords))
                    .unwrap()
            }
            Validated::Date(_)
            | Validated::Time(_)
            | Validated::Duration(_)
            | Validated::Decimal(_)
            | Validated::Net(..)
            | Validated::Bytes(_) => python_wire_value(py, value),
            Validated::List(items) => {
                let list = PyList::empty(py);
                for item in items {
                    list.append(python_value(py, item)).unwrap();
                }
                list.into_any()
            }
            Validated::Object(fields) => {
                let dict = PyDict::new(py);
                for (name, item) in fields {
                    dict.set_item(name, python_value(py, item)).unwrap();
                }
                dict.into_any()
            }
        }
    }

    /// The Python object for a date, time, duration, decimal or bytes value.
    fn python_wire_value<'py>(py: Python<'py>, value: &Validated) -> Bound<'py, PyAny> {
        let datetime = py.import("datetime").unwrap();
        match value {
            Validated::Date(day) => datetime
                .getattr("date")
                .unwrap()
                .call1((day.year(), u8::from(day.month()), day.day()))
                .unwrap(),
            Validated::Time(clock) => {
                let zone = clock.offset.map_or_else(
                    || py.None().into_bound(py),
                    |seconds| {
                        let delta = datetime
                            .getattr("timedelta")
                            .unwrap()
                            .call1((0, seconds))
                            .unwrap();
                        datetime
                            .getattr("timezone")
                            .unwrap()
                            .call1((delta,))
                            .unwrap()
                    },
                );
                let keywords = PyDict::new(py);
                keywords.set_item("tzinfo", zone).unwrap();
                datetime
                    .getattr("time")
                    .unwrap()
                    .call(
                        (clock.hour, clock.minute, clock.second, clock.micro),
                        Some(&keywords),
                    )
                    .unwrap()
            }
            Validated::Duration(span) => datetime
                .getattr("timedelta")
                .unwrap()
                .call1((span.days, span.seconds, span.micros))
                .unwrap(),
            Validated::Decimal(text) => py
                .import("decimal")
                .unwrap()
                .getattr("Decimal")
                .unwrap()
                .call1((text,))
                .unwrap(),
            Validated::Bytes(raw) => PyBytes::new(py, raw).into_any(),
            Validated::Net(kind, text) => py
                .import("ipaddress")
                .unwrap()
                .getattr(kind.class_name())
                .unwrap()
                .call1((text,))
                .unwrap(),
            _ => unreachable!("not a wire value"),
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Verdict {
        Same,
        Deferred,
    }

    fn repr(value: &Bound<'_, PyAny>) -> String {
        value.repr().unwrap().to_string()
    }

    fn compare_data(
        py: Python<'_>,
        compiled: &Compiled,
        python: &Bound<'_, PyAny>,
        native: &Json,
        options: &Options,
    ) -> Verdict {
        let skip: Vec<String> = options.skip_fields.iter().cloned().collect();
        let (expected, expected_problems) = compiled
            .validate(
                py,
                python,
                false,
                options.use_aliases,
                options.allow_naive_datetime,
                skip,
                Some(options.input_bounds),
            )
            .unwrap();
        let schema = compiled.native().expect("the schema lowers");
        let Ok((value, problems)) = schema.validate(native, options) else {
            return Verdict::Deferred;
        };
        let shown = repr(python);
        assert_eq!(problems, expected_problems, "{shown}");
        match (value, expected) {
            (None, None) => {}
            (Some(value), Some(expected)) => {
                let rebuilt = python_value(py, &value);
                assert_eq!(repr(&rebuilt), repr(&expected), "{shown}");
                assert!(rebuilt.eq(&expected).unwrap(), "{shown}");
            }
            (value, expected) => panic!("{shown}: {value:?} vs {expected:?}"),
        }
        Verdict::Same
    }

    fn compare_text(py: Python<'_>, compiled: &Compiled, text: &str, options: &Options) -> Verdict {
        let python = decode_json(py, text.as_bytes()).unwrap();
        let document = decode_json_document(text.as_bytes()).unwrap();
        compare_data(py, compiled, &python, &document.root, options)
    }

    /// Run every body under every option set; return how many were compared
    /// (not deferred).
    fn compare_all(
        py: Python<'_>,
        compiled: &Compiled,
        bodies: &[&str],
        sets: &[Options],
    ) -> usize {
        let mut same = 0;
        for options in sets {
            for body in bodies {
                if compare_text(py, compiled, body, options) == Verdict::Same {
                    same += 1;
                }
            }
        }
        same
    }

    fn option_sets() -> Vec<Options> {
        vec![
            options(true, false, &[]),
            options(false, false, &[]),
            options(true, true, &[]),
            options(true, false, &["count", "user_name", "items", "pet", "n"]),
        ]
    }

    const SCALAR_BODIES: [&str; 46] = [
        r#"{"name": "abc", "count": 3}"#,
        r#"{"name": "", "count": 3}"#,
        r#"{"name": "abcdef", "count": 3}"#,
        r#"{"name": "ABC", "count": 3}"#,
        r#"{"name": "é'é", "count": 0}"#,
        r#"{"name": 5, "count": "3"}"#,
        r#"{"name": null, "count": null}"#,
        r#"{"count": 11}"#,
        r#"{"count": -1, "name": "a"}"#,
        r#"{"count": 1.0, "name": "a"}"#,
        r#"{"count": true, "name": "a"}"#,
        r#"{"count": 99999999999999999999, "name": 99999999999999999999}"#,
        r#"{"name": "a", "count": 1, "ratio": 0}"#,
        r#"{"name": "a", "count": 1, "ratio": 1}"#,
        r#"{"name": "a", "count": 1, "ratio": 1.5}"#,
        r#"{"name": "a", "count": 1, "ratio": -0.0, "flag": 1}"#,
        r#"{"name": "a", "count": 1, "ratio": 1e308, "flag": false}"#,
        r#"{"name": "a", "count": 1, "ratio": true, "flag": "true"}"#,
        r#"{"name": "a", "count": 1, "key": "12345678-1234-5678-1234-567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "ABCDEF00-1234-5678-1234-567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "12345678123456781234567812345678"}"#,
        r#"{"name": "a", "count": 1, "key": "{12345678-1234-5678-1234-567812345678}"}"#,
        r#"{"name": "a", "count": 1, "key": 7}"#,
        r#"{"name": "a", "count": 1, "note": "xy", "kind": "b'c"}"#,
        r#"{"name": "a", "count": 1, "note": null, "kind": "é\"x"}"#,
        r#"{"name": "a", "count": 1, "note": "é", "kind": "z"}"#,
        r#"{"name": "a", "count": 1, "kind": 1, "level": 1}"#,
        r#"{"name": "a", "count": 1, "level": 2, "yes": true}"#,
        r#"{"name": "a", "count": 1, "level": 3, "yes": false}"#,
        r#"{"name": "a", "count": 1, "level": 7.0, "yes": 1}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740993}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740994}"#,
        r#"{"name": "a", "count": 1, "weight": 9007199254740992.0}"#,
        r#"{"name": "a", "count": 1, "weight": 1e300, "zero": 0}"#,
        r#"{"name": "a", "count": 1, "weight": 3, "zero": -0.0}"#,
        r#"{"name": "a", "count": 1, "zero": 0.5, "small": 0}"#,
        r#"{"name": "a", "count": 1, "zero": 1, "small": 1}"#,
        r#"{"name": "a", "count": 1, "small": 2, "nan": 1}"#,
        r#"{"name": "a", "count": 1, "nan": 1e308, "edge": 9223372036854775807}"#,
        r#"{"name": "a", "count": 1, "edge": -9223372036854775808, "wide": 9223372036854775807}"#,
        r#"{"name": "a", "count": 1, "wide": -9223372036854775808, "digit": 11}"#,
        r#"{"name": "a", "count": 1, "digit": 12, "inf": 1e308}"#,
        r#"{"name": "a", "count": 1, "inf": -1e308, "extra": 1, "ÄÖÜ": 2}"#,
        r#"{"name": "a", "count": 1, "name": "abcdef", "count": 2}"#,
        r#"[]"#,
        r#""text""#,
    ];

    const DATETIMES: [&str; 22] = [
        "2026-11-13T10:00:00Z",
        "2026-11-13T10:00:00+01:00",
        "2026-11-13T10:00:00-05:30",
        "2026-11-13T10:00:00-00:00",
        "2026-11-13T10:00:00+23:59",
        "2026-11-13T10:00:00.1Z",
        "2026-11-13T10:00:00.123456+02:00",
        "2026-11-13T10:00:00.1234567Z",
        "2026-11-13T10:00:00",
        "2026-11-13T23:59:59.999999",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "2024-02-29T00:00:00Z",
        "2026-02-29T00:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-11-13T24:00:00Z",
        "2026-11-13T10:00:60Z",
        "2026-11-13T10:00:00+24:00",
        "2026-11-13t10:00:00Z",
        "2026-11-13T10:00:00z",
        "2026-11-13",
        "2026-11-13 10:00:00Z",
    ];

    #[test]
    fn scalar_constraints_report_the_same_problems_and_values() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let same = compare_all(py, &compiled, &SCALAR_BODIES, &option_sets());
            // Only the body with an integer outside i64 in `count` defers,
            // except where `count` is skipped.
            assert_eq!(same, SCALAR_BODIES.len() * option_sets().len() - 3);
        });
    }

    #[test]
    fn datetimes_match_or_defer_and_never_disagree() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let mut same = 0;
            for text in DATETIMES {
                for allow_naive in [false, true] {
                    let body = format!(r#"{{"name": "a", "count": 1, "at": "{text}"}}"#);
                    let options = options(true, allow_naive, &[]);
                    if compare_text(py, &compiled, &body, &options) == Verdict::Same {
                        same += 1;
                    }
                }
            }
            // Twelve certain shapes in both modes, two rejected by shape.
            assert_eq!(same, 2 * 14);
            let shaped = r#"{"name": "a", "count": 1, "at": "2026-02-29T00:00:00Z"}"#;
            let document = decode_json_document(shaped.as_bytes()).unwrap();
            let native = compiled.native().unwrap();
            assert_eq!(
                native.validate(&document.root, &options(true, false, &[])),
                Err(UNCERTAIN_DATETIME)
            );
        });
    }

    #[test]
    fn aliases_and_unknown_fields_report_the_same_problems() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, ALIASES);
            let long_key = "é".repeat(80);
            let long = format!(r#"{{"userName": "a", "{long_key}": 1}}"#);
            let bodies = [
                r#"{"userName": "a"}"#,
                r#"{"user_name": "a"}"#,
                r#"{"userName": "a", "user_name": "b"}"#,
                r#"{"user_name": "b", "userName": 5, "age": null}"#,
                r#"{"userName": "a", "age": "1", "other": 1, "x\"y": 2}"#,
                r#"{}"#,
                long.as_str(),
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn nested_objects_and_lists_report_the_same_paths() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, NESTED);
            let bodies = [
                r#"{"items": [{"id": 1}]}"#,
                r#"{"items": []}"#,
                r#"{"items": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]}"#,
                r#"{"items": [{"id": "1", "label": "x"}, 5, {"label": "ok", "more": 1}]}"#,
                r#"{"items": {"id": 1}, "child": [], "tags": "ab"}"#,
                r#"{"items": [{"id": 1}], "child": null, "tags": null}"#,
                r#"{"items": [{"id": 1}], "child": {"id": 2, "label": "é"}, "tags": ["ab", "abc", 1]}"#,
                r#"{"items": [{"id": 1}], "grid": [[0, 1], [2, -1], [1, 2, 3], 4], "x": 1}"#,
                r#"{"items": [{"id": 1}], "grid": []}"#,
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn tagged_unions_report_the_same_problems() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, UNION);
            let bodies = [
                r#"{"pet": {"type": "cat", "lives": 3}}"#,
                r#"{"pet": {"type": "dog", "good": true, "extra": 1}}"#,
                r#"{"pet": {"type": "cow"}}"#,
                r#"{"pet": {"type": 5}}"#,
                r#"{"pet": {"lives": 3}}"#,
                r#"{"pet": {"kind": "cat", "type": "cat", "lives": 3}}"#,
                r#"{"pet": {"kind": "cat", "lives": 3}}"#,
                r#"{"pet": {"type": "cat", "lives": 10, "x": 1}}"#,
                r#"{"pet": [], "pets": [null, {"kind": "dog"}, {"type": "dog"}, 3]}"#,
                r#"{"pet": {"type": "dog"}, "pets": [{"kind": "cat", "type": "cat", "lives": 1}]}"#,
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            assert_eq!(same, bodies.len() * option_sets().len());
        });
    }

    #[test]
    fn mappings_report_the_same_problems_and_values() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, MAPPING);
            let long_key = "k".repeat(70);
            let long = format!(
                r#"{{"labels": {{"{long_key}": "x"}}, "points": {{"{long_key}": {{"x": 1}}}}}}"#
            );
            let bodies = [
                r#"{"labels": {"b": "x", "a": "yy"}}"#,
                r#"{"labels": {"b": "x", "a": "yy", "b": "z"}}"#,
                r#"{"labels": {}}"#,
                r#"{"labels": {"a": "x", "b": "x", "c": "x", "d": "x"}}"#,
                r#"{"labels": {"a": 1, "b": "toolong", "c": null}}"#,
                r#"{"labels": {"abcde": "x", "__proto__": "y"}}"#,
                r#"{"labels": [], "points": "x", "grid": 3}"#,
                r#"{"labels": {"a": "x"}, "points": {"ab": {"x": 1}, "a": {"x": 2}, "AB": {"x": 3}}}"#,
                r#"{"labels": {"a": "x"}, "points": {"ab": {"y": -1, "z": 1}, "cd": 5}}"#,
                r#"{"labels": {"a": "x"}, "grid": null}"#,
                r#"{"labels": {"a": "x"}, "grid": {}}"#,
                r#"{"labels": {"a": "x"}, "grid": {"r": [1, "2"], "n": null, "e": []}}"#,
                r#"{"labels": {"é": "é"}, "grid": {"r": [99999999999999999999]}}"#,
                long.as_str(),
            ];
            let same = compare_all(py, &compiled, &bodies, &option_sets());
            // Only the integer outside i64 inside `grid` defers.
            assert_eq!(
                same,
                bodies.len() * option_sets().len() - option_sets().len()
            );
        });
    }

    #[test]
    fn the_error_cap_and_the_depth_limit_are_shared() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, ALIASES);
            let mut object = JsonObject::default();
            for index in 0..60 {
                object.insert(format!("unknown{index}"), Json::Int(index));
            }
            let many = Json::Object(object);
            let python = json_to_python(py, &many).unwrap();
            let options = options(true, false, &[]);
            assert_eq!(
                compare_data(py, &compiled, &python, &many, &options),
                Verdict::Same
            );
            let (_, problems) = compiled
                .native()
                .unwrap()
                .validate(&many, &options)
                .unwrap();
            assert_eq!(problems.len(), MAX_ERRORS + 1);
            assert_eq!(problems[MAX_ERRORS].1, "too_many_errors");

            let chain = compile(py, CHAIN);
            for levels in [127, 128, 129, 140] {
                let mut value = Json::Object(JsonObject::default());
                for _ in 0..levels {
                    let mut outer = JsonObject::default();
                    outer.insert("next".into(), value);
                    value = Json::Object(outer);
                }
                let python = json_to_python(py, &value).unwrap();
                assert_eq!(
                    compare_data(py, &chain, &python, &value, &options),
                    Verdict::Same
                );
                let (_, problems) = chain.native().unwrap().validate(&value, &options).unwrap();
                let deep = problems.first().is_some_and(|problem| problem.1 == "depth");
                assert_eq!(deep, levels > MAX_DEPTH, "{levels}");
            }
        });
    }

    #[test]
    fn integers_outside_i64_defer_only_where_python_reads_them_as_numbers() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, SCALARS);
            let options = options(true, false, &[]);
            let text_field = r#"{"name": 99999999999999999999, "count": 1}"#;
            assert_eq!(
                compare_text(py, &compiled, text_field, &options),
                Verdict::Same
            );
            for body in [
                r#"{"name": "a", "count": 99999999999999999999}"#,
                r#"{"name": "a", "count": 1, "ratio": 99999999999999999999}"#,
            ] {
                assert_eq!(
                    compare_text(py, &compiled, body, &options),
                    Verdict::Deferred
                );
            }
        });
    }

    #[test]
    fn constraints_without_an_exact_rust_equivalent_are_not_lowered() {
        Python::initialize();
        Python::attach(|py| {
            for constraint in [
                r#"node("int", ge=decimal.Decimal(1))"#,
                r#"node("int", ge=True)"#,
                r#"node("int", le=2**70)"#,
                r#"node("str", pattern=Text("a+"))"#,
                r#"node("str", one_of=("a", Text("b")))"#,
                r#"node("str", one_of=("\ud800",))"#,
                r#"node("int", one_of=(1, 2**70))"#,
                r#"node("int", one_of=(None,))"#,
                r#"node("uuid", one_of=("12345678-1234-5678-1234-567812345678",))"#,
                r#"node("list", item=node("int", ge=decimal.Decimal(1)))"#,
                r#"node("dict", item=node("int", ge=decimal.Decimal(1)), key=node("str"))"#,
                r#"node("dict", item=node("int"), key=node("str", pattern=Text("a+")))"#,
            ] {
                let table = format!(r#"[(Made, [("f", "f", {constraint}, True)], False)]"#);
                assert!(compile(py, &table).native().is_none(), "{constraint}");
            }
            for constraint in [
                r#"node("int", ge=-1, le=2.5, gt=-2**63, lt=2**63 - 1)"#,
                r#"node("str", pattern="a+", one_of=("a", "aa"))"#,
                r#"node("bool", one_of=(True, 0, 1.0))"#,
                r#"node("datetime", ge=decimal.Decimal(1))"#,
            ] {
                let table = format!(r#"[(Made, [("f", "f", {constraint}, True)], False)]"#);
                let lowered = compile(py, &table);
                assert!(lowered.native().is_some(), "{constraint}");
            }
        });
    }

    #[test]
    fn python_number_comparison_is_exact_across_int_and_float() {
        let big = Number::Int(9_007_199_254_740_993);
        assert_eq!(
            compare_numbers(big, Number::Float(9_007_199_254_740_992.0)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Float(9_007_199_254_740_992.0), big),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MAX), Number::Float(TWO_POW_63)),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MIN), Number::Float(-TWO_POW_63)),
            Some(Ordering::Equal)
        );
        assert_eq!(
            compare_numbers(Number::Int(i64::MIN), Number::Float(f64::NEG_INFINITY)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Int(-1), Number::Float(-1.5)),
            Some(Ordering::Greater)
        );
        assert_eq!(
            compare_numbers(Number::Int(-2), Number::Float(-1.5)),
            Some(Ordering::Less)
        );
        assert_eq!(
            compare_numbers(Number::Int(0), Number::Float(-0.0)),
            Some(Ordering::Equal)
        );
        assert_eq!(
            compare_numbers(Number::Int(0), Number::Float(f64::NAN)),
            None
        );
        assert!(holds(CompareOp::Ne, None));
        assert!(!holds(CompareOp::Ge, None));
        assert!(holds(CompareOp::Ne, Some(Ordering::Less)));
    }

    /// A small deterministic generator (xorshift64).
    struct Draw(u64);

    impl Draw {
        fn next(&mut self, below: usize) -> usize {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            usize::try_from(self.0 % u64::try_from(below).unwrap()).unwrap()
        }
    }

    const VALUES: [&str; 44] = [
        "null",
        "true",
        "false",
        "0",
        "-1",
        "1",
        "2",
        "3",
        "7",
        "11",
        "1.5",
        "0.5",
        "-0.0",
        "1e308",
        "9007199254740993",
        "9223372036854775807",
        "\"\"",
        "\"a\"",
        "\"ab\"",
        "\"abcdef\"",
        "\"é'\"",
        "\"b'c\"",
        "\"é\\\"x\"",
        "\"cat\"",
        "\"dog\"",
        "\"12345678-1234-5678-1234-567812345678\"",
        "\"2026-11-13T10:00:00Z\"",
        "\"2026-11-13T10:00:00\"",
        "\"2026-11-13T10:00:00.5+01:00\"",
        "[]",
        "[1]",
        "{}",
        "[\"ab\", \"abc\"]",
        "{\"id\": 1}",
        "{\"id\": \"x\", \"label\": \"a\"}",
        "[{\"id\": 1}, {\"id\": 2, \"label\": \"no\"}]",
        "{\"type\": \"cat\", \"lives\": 3}",
        "{\"type\": \"dog\", \"good\": 1}",
        "{\"type\": 5}",
        "{\"kind\": \"cat\", \"type\": \"dog\"}",
        "[[1, 2], [3, -1]]",
        "[null, {\"kind\": \"dog\"}]",
        "{\"next\": {\"next\": null, \"n\": 1}}",
        "\"\\u00e9\\ud83d\\ude00\"",
    ];

    const KEYS: [&str; 34] = [
        "name",
        "count",
        "ratio",
        "flag",
        "key",
        "at",
        "note",
        "kind",
        "level",
        "yes",
        "weight",
        "zero",
        "small",
        "nan",
        "edge",
        "wide",
        "digit",
        "inf",
        "userName",
        "user_name",
        "age",
        "items",
        "child",
        "tags",
        "grid",
        "pet",
        "pets",
        "next",
        "n",
        "type",
        "extra",
        "é",
        "labels",
        "points",
    ];

    /// Values a `dict` field of [`MAPPING`] accepts or nearly accepts.
    const MAP_VALUES: [&str; 12] = [
        "{}",
        "{\"a\": \"x\"}",
        "{\"a\": \"x\", \"bb\": \"yy\"}",
        "{\"__proto__\": \"z\"}",
        "{\"a\": 1}",
        "{\"abcde\": \"x\"}",
        "{\"ab\": {\"x\": 1}}",
        "{\"ab\": {\"x\": 1, \"y\": 2}, \"cd\": {\"x\": -3}}",
        "{\"A\": {\"x\": 1}}",
        "{\"r\": [1, 2], \"n\": null}",
        "{\"r\": [\"1\"]}",
        "{\"a\": \"x\", \"a\": \"yy\"}",
    ];

    const MAP_KEYS: [&str; 4] = ["labels", "labels", "points", "grid"];

    fn random_body(draw: &mut Draw) -> String {
        random_body_of(draw, &KEYS, &VALUES)
    }

    fn random_body_of(draw: &mut Draw, keys: &[&str], values: &[&str]) -> String {
        let members: Vec<String> = (0..draw.next(7))
            .map(|_| {
                let key = keys[draw.next(keys.len())];
                let value = values[draw.next(values.len())];
                format!("\"{key}\": {value}")
            })
            .collect();
        format!("{{{}}}", members.join(", "))
    }

    #[test]
    fn random_documents_never_disagree() {
        Python::initialize();
        Python::attach(|py| {
            let mut draw = Draw(0x9E37_79B9_7F4A_7C15);
            for definitions in [SCALARS, ALIASES, NESTED, UNION, MAPPING, CHAIN] {
                let compiled = compile(py, definitions);
                let mut same = 0;
                let sets = option_sets();
                for round in 0..600 {
                    let body = random_body(&mut draw);
                    if compare_text(py, &compiled, &body, &sets[round % sets.len()])
                        == Verdict::Same
                    {
                        same += 1;
                    }
                }
                assert!(same > 450, "{same} of 600 compared");
            }
        });
    }

    /// Whether a native result holds a non-empty accepted `labels` map.
    fn accepted_map(value: Option<&Validated>) -> bool {
        let Some(Validated::Object(fields)) = value else {
            return false;
        };
        fields.iter().any(|(name, item)| {
            name == "labels" && matches!(item, Validated::Object(entries) if !entries.is_empty())
        })
    }

    #[test]
    fn random_mapping_documents_never_disagree_and_are_often_accepted() {
        Python::initialize();
        Python::attach(|py| {
            let mut draw = Draw(0x2545_F491_4F6C_DD1D);
            let compiled = compile(py, MAPPING);
            let native = compiled.native().unwrap();
            // Junk values are drawn by `random_documents_never_disagree`; these
            // stay close to valid so accepted maps are reached too.
            let values: Vec<&str> = MAP_VALUES.iter().chain(&VALUES[..4]).copied().collect();
            let sets = option_sets();
            let (mut same, mut accepted) = (0, 0);
            for round in 0..600 {
                let body = random_body_of(&mut draw, &MAP_KEYS, &values);
                let options = &sets[round % sets.len()];
                if compare_text(py, &compiled, &body, options) == Verdict::Same {
                    same += 1;
                }
                let document = decode_json_document(body.as_bytes()).unwrap();
                if let Ok((value, _)) = native.validate(&document.root, options) {
                    accepted += usize::from(accepted_map(value.as_ref()));
                }
            }
            assert!(same > 450, "{same} of 600 compared");
            assert!(accepted > 10, "{accepted} accepted non-empty maps");
        });
    }

    const BOUNDED: &str = r#"[(Made, [
        ("caps", "caps", node("dict", item=node("int"), key=node("str", input_max_length=3),
                              input_max_length=2), True),
        ("open", "open", node("dict", nullable=True, item=node("int"), key=node("str", max_length=5,
                              input_max_length=3), max_length=4, input_max_length=1), False),
    ], True)]"#;

    #[test]
    fn wire_input_bounds_apply_only_when_asked_on_both_paths() {
        Python::initialize();
        Python::attach(|py| {
            let compiled = compile(py, BOUNDED);
            let bodies = [
                r#"{"caps": {"a": 1, "b": 2}}"#,
                r#"{"caps": {"a": 1, "b": 2, "c": 3}}"#,
                r#"{"caps": {"abcd": 1}}"#,
                r#"{"caps": {}, "open": {"abcd": 1, "b": 2, "c": 3, "d": 4}}"#,
                r#"{"caps": {}, "open": {"abcdef": 1, "b": 2, "c": 3, "d": 4, "e": 5}}"#,
            ];
            let mut wire = options(true, false, &[]);
            wire.input_bounds = true;
            let sets = [options(true, false, &[]), wire];
            let same = compare_all(py, &compiled, &bodies, &sets);
            assert_eq!(same, bodies.len() * sets.len());
            let native = compiled.native().unwrap();
            let problems = |text: &str, options: &Options| {
                let document = decode_json_document(text.as_bytes()).unwrap();
                native.validate(&document.root, options).unwrap().1
            };
            assert!(problems(bodies[1], &sets[0]).is_empty());
            assert_eq!(problems(bodies[1], &sets[1])[0].1, "max_length");
            assert_eq!(problems(bodies[2], &sets[1])[0].1, "key_max_length");
            // An explicit max_length wins over the wire-input bound.
            assert!(problems(bodies[3], &sets[1]).is_empty());
            let codes: Vec<&str> = problems(bodies[4], &sets[0]).iter().map(|p| p.1).collect();
            assert_eq!(codes, ["max_length"]);
        });
    }
}
