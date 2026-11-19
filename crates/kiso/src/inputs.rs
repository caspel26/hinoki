//! Conversion of path and query text into typed handler arguments.
//!
//! Each converter mirrors the Python converter of the same name in
//! `hinoki.runtime.binding` (`to_int`, `to_float`, `to_bool`, `to_uuid`), so a
//! request produces the same value or the same "invalid value" error whichever
//! lane serves it. An input this layer cannot decide exactly as Python does is
//! deferred: the request then takes the Python lane unchanged.

use crate::plan::InputKind;

/// Longest integer, in digits, that always fits `i64` (`i64::MAX` has 19).
const MAX_NATIVE_INT_DIGITS: usize = 18;
const UUID_LEN: usize = 36;

#[derive(Debug, Clone, PartialEq)]
pub enum InputValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    /// Canonical hyphenated text; Python builds the `UUID` object.
    Uuid(String),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Converted {
    Value(InputValue),
    /// Python would answer `422 type`.
    Invalid,
    /// Python's own limits decide (very long integers); use the Python lane.
    Defer,
}

pub fn convert(kind: InputKind, raw: String) -> Converted {
    match kind {
        InputKind::Str => Converted::Value(InputValue::Str(raw)),
        InputKind::Int => int(&raw),
        InputKind::Float => float(&raw),
        InputKind::Bool => bool_value(&raw),
        InputKind::Uuid => uuid(raw),
    }
}

fn int(raw: &str) -> Converted {
    let digits = raw.strip_prefix('-').unwrap_or(raw);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Converted::Invalid;
    }
    if digits.len() > MAX_NATIVE_INT_DIGITS {
        return Converted::Defer;
    }
    raw.parse()
        .map_or(Converted::Invalid, |v| Converted::Value(InputValue::Int(v)))
}

/// `-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?`, then finite.
fn is_decimal(raw: &str) -> bool {
    fn digits(text: &str) -> Option<&str> {
        let end = text.bytes().take_while(u8::is_ascii_digit).count();
        (end > 0).then(|| &text[end..])
    }
    let rest = raw.strip_prefix('-').unwrap_or(raw);
    let Some(mut rest) = digits(rest) else {
        return false;
    };
    if let Some(fraction) = rest.strip_prefix('.') {
        let Some(after) = digits(fraction) else {
            return false;
        };
        rest = after;
    }
    if let Some(exponent) = rest.strip_prefix(['e', 'E']) {
        let exponent = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        return digits(exponent) == Some("");
    }
    rest.is_empty()
}

fn float(raw: &str) -> Converted {
    if !is_decimal(raw) {
        return Converted::Invalid;
    }
    match raw.parse::<f64>() {
        Ok(value) if value.is_finite() => Converted::Value(InputValue::Float(value)),
        _ => Converted::Invalid,
    }
}

fn bool_value(raw: &str) -> Converted {
    match raw {
        "true" | "1" => Converted::Value(InputValue::Bool(true)),
        "false" | "0" => Converted::Value(InputValue::Bool(false)),
        _ => Converted::Invalid,
    }
}

/// Only the canonical 8-4-4-4-12 form, in either case.
fn uuid(raw: String) -> Converted {
    let canonical = raw.len() == UUID_LEN
        && raw.bytes().enumerate().all(|(at, b)| match at {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        });
    if canonical {
        Converted::Value(InputValue::Uuid(raw))
    } else {
        Converted::Invalid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn value(kind: InputKind, raw: &str) -> Converted {
        convert(kind, raw.to_owned())
    }

    #[test]
    fn integers_follow_the_python_pattern() {
        assert_eq!(
            value(InputKind::Int, "-007"),
            Converted::Value(InputValue::Int(-7))
        );
        for bad in ["", "-", "+1", "1.0", " 1", "1 ", "1_0", "０１", "0x10"] {
            assert_eq!(value(InputKind::Int, bad), Converted::Invalid, "{bad:?}");
        }
        assert_eq!(
            value(InputKind::Int, "999999999999999999"),
            Converted::Value(InputValue::Int(999_999_999_999_999_999))
        );
        assert_eq!(
            value(InputKind::Int, "1000000000000000000"),
            Converted::Defer
        );
        assert_eq!(value(InputKind::Int, &"9".repeat(5000)), Converted::Defer);
    }

    #[test]
    fn floats_follow_the_python_pattern_and_must_be_finite() {
        for (raw, expected) in [
            ("2.5", 2.5),
            ("-0", -0.0),
            ("1e3", 1000.0),
            ("1.5E-2", 0.015),
        ] {
            assert_eq!(
                value(InputKind::Float, raw),
                Converted::Value(InputValue::Float(expected)),
                "{raw:?}"
            );
        }
        for bad in [
            "", ".5", "5.", "1e", "1e+", "e3", "nan", "inf", "-inf", "1e999", "+1", "1_0", "1.5.5",
        ] {
            assert_eq!(value(InputKind::Float, bad), Converted::Invalid, "{bad:?}");
        }
    }

    #[test]
    fn booleans_accept_only_the_four_spellings() {
        for (raw, expected) in [("true", true), ("1", true), ("false", false), ("0", false)] {
            assert_eq!(
                value(InputKind::Bool, raw),
                Converted::Value(InputValue::Bool(expected))
            );
        }
        for bad in ["", "True", "yes", "2", "TRUE"] {
            assert_eq!(value(InputKind::Bool, bad), Converted::Invalid, "{bad:?}");
        }
    }

    #[test]
    fn uuids_must_be_canonical() {
        let good = "123e4567-E89b-12d3-a456-426614174000";
        assert_eq!(
            value(InputKind::Uuid, good),
            Converted::Value(InputValue::Uuid(good.into()))
        );
        for bad in [
            "123e4567e89b12d3a456426614174000",
            "{123e4567-e89b-12d3-a456-426614174000}",
            "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
            "123e4567-e89b-12d3-a456-42661417400",
            "123e4567-e89b-12d3-a456-42661417400g",
            "",
        ] {
            assert_eq!(value(InputKind::Uuid, bad), Converted::Invalid, "{bad:?}");
        }
    }
}
