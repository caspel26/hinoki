//! Wire forms of the `date`, `time`, `timedelta`, `Decimal` and `bytes` field
//! kinds: strict parsers shared by both validation paths (so the Python-object
//! path and the GIL-free path cannot read a text differently) and the
//! formatters the JSON encoders use.
//!
//! Every parser accepts ASCII only: no other Unicode digits, no signs or
//! whitespace that the grammar does not name, no `_` separators.
//!
//! * date: `YYYY-MM-DD`, a real calendar date from year 1 to 9999.
//! * time: `HH:MM:SS[.ffffff]` then `Z` or `+HH:MM`/`-HH:MM` (an offset is
//!   required on wire input, as for datetimes; see `time_tz`).
//! * duration: `[-]P[nD][T[nH][nM][n[.ffffff]S]]`, at least one component,
//!   within Python's `timedelta` range. Years, months and weeks are refused:
//!   their length depends on a calendar.
//! * decimal: `-?(0|[1-9][0-9]*)(\.[0-9]+)?`, no exponent, so every digit of
//!   the value is written out and bounded by the digit limit.
//! * bytes: standard, padded, canonical base64 (RFC 4648 section 4).

use std::cmp::Ordering;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use time::{Date, Month};

/// Python's `timedelta` limit, in days, in both directions.
const MAX_DURATION_DAYS: i128 = 999_999_999;
const SECONDS_PER_DAY: i128 = 86_400;
const MICROS_PER_SECOND: i128 = 1_000_000;
/// Fraction digits a time or a duration keeps: microseconds.
const MAX_FRACTION_DIGITS: usize = 6;
/// Significant digits of one duration component beyond which the value is
/// out of range whatever its unit (`10^15` seconds exceeds the limit too);
/// bounds the arithmetic.
const MAX_DURATION_COMPONENT_DIGITS: usize = 15;
const MAX_HOUR: u32 = 23;
const MAX_MINUTE: u32 = 59;
/// The most digits any `Decimal` field may declare (`max_digits`); the JSON
/// encoders refuse to write a `Decimal` with more.
pub(crate) const DECIMAL_DIGITS_CEILING: usize = 1000;
const DATE_LENGTH: usize = 10;
const CLOCK_LENGTH: usize = 8;

fn digits_value(bytes: &[u8]) -> u32 {
    bytes
        .iter()
        .fold(0, |total, digit| total * 10 + u32::from(digit - b'0'))
}

fn all_digits(bytes: &[u8]) -> bool {
    bytes.iter().all(u8::is_ascii_digit)
}

/// `YYYY-MM-DD`, or `None`.
pub(crate) fn parse_date(text: &str) -> Option<Date> {
    let bytes = text.as_bytes();
    if bytes.len() != DATE_LENGTH || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    let (year, month, day) = (&bytes[..4], &bytes[5..7], &bytes[8..]);
    if !(all_digits(year) && all_digits(month) && all_digits(day)) {
        return None;
    }
    let year = i32::try_from(digits_value(year)).ok()?;
    if year == 0 {
        return None;
    }
    let month = Month::try_from(u8::try_from(digits_value(month)).ok()?).ok()?;
    let day = u8::try_from(digits_value(day)).ok()?;
    Date::from_calendar_date(year, month, day).ok()
}

/// A validated time of day and its UTC offset in seconds (`None`: no offset).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ClockTime {
    pub(crate) hour: u8,
    pub(crate) minute: u8,
    pub(crate) second: u8,
    pub(crate) micro: u32,
    pub(crate) offset: Option<i32>,
}

/// `.f` to `.ffffff` at `at`: the microseconds and where the text continues;
/// `None` when a `.` is not followed by 1 to 6 digits.
fn fraction(bytes: &[u8], at: usize) -> Option<(u32, usize)> {
    if bytes.get(at) != Some(&b'.') {
        return Some((0, at));
    }
    let start = at + 1;
    let count = bytes[start..]
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if count == 0 || count > MAX_FRACTION_DIGITS {
        return None;
    }
    let scale = 10_u32.pow(u32::try_from(MAX_FRACTION_DIGITS - count).ok()?);
    Some((
        digits_value(&bytes[start..start + count]) * scale,
        start + count,
    ))
}

/// `Z`, `+HH:MM` or `-HH:MM` within a day, as seconds; `Some(None)` for none.
fn offset(rest: &[u8]) -> Option<Option<i32>> {
    let (sign, digits) = match rest {
        [] => return Some(None),
        [b'Z'] => return Some(Some(0)),
        [b'+', digits @ ..] => (1, digits),
        [b'-', digits @ ..] => (-1, digits),
        _ => return None,
    };
    let [h1, h2, b':', m1, m2] = digits else {
        return None;
    };
    if !all_digits(&[*h1, *h2, *m1, *m2]) {
        return None;
    }
    let (hours, minutes) = (digits_value(&[*h1, *h2]), digits_value(&[*m1, *m2]));
    if hours > MAX_HOUR || minutes > MAX_MINUTE {
        return None;
    }
    let seconds = i32::try_from(hours * 3600 + minutes * 60).ok()?;
    Some(Some(sign * seconds))
}

/// `HH:MM:SS[.ffffff][Z|±HH:MM]`, or `None`.
pub(crate) fn parse_time(text: &str) -> Option<ClockTime> {
    let bytes = text.as_bytes();
    if bytes.len() < CLOCK_LENGTH || bytes[2] != b':' || bytes[5] != b':' {
        return None;
    }
    let parts = [&bytes[..2], &bytes[3..5], &bytes[6..8]];
    if !parts.iter().all(|part| all_digits(part)) {
        return None;
    }
    let [hour, minute, second] = parts.map(digits_value);
    if hour > MAX_HOUR || minute > MAX_MINUTE || second > MAX_MINUTE {
        return None;
    }
    let (micro, at) = fraction(bytes, CLOCK_LENGTH)?;
    let offset = offset(&bytes[at..])?;
    Some(ClockTime {
        hour: u8::try_from(hour).ok()?,
        minute: u8::try_from(minute).ok()?,
        second: u8::try_from(second).ok()?,
        micro,
        offset,
    })
}

/// A duration as Python normalizes a `timedelta`: `0 <= seconds < 86400`,
/// `0 <= micros < 1000000`, `days` carrying the sign.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Duration {
    pub(crate) days: i64,
    pub(crate) seconds: i64,
    pub(crate) micros: i64,
}

/// Why a duration text was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DurationError {
    /// Not the accepted ISO 8601 form (`duration`).
    Shape,
    /// Well formed but outside the `timedelta` range (`duration_range`).
    Range,
}

/// Reads the duration components after `P`, in order, totalling microseconds.
struct DurationReader<'t> {
    bytes: &'t [u8],
    at: usize,
    total: i128,
    components: usize,
    too_large: bool,
}

impl DurationReader<'_> {
    /// The digits at the cursor, when they end with `unit`: consumed and
    /// added as `scale` seconds each.
    fn component(&mut self, unit: u8, scale: i128) -> bool {
        let count = self.bytes[self.at..]
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
        if count == 0 || self.bytes.get(self.at + count) != Some(&unit) {
            return false;
        }
        self.add(self.at, self.at + count, scale * MICROS_PER_SECOND);
        self.at += count + 1;
        self.components += 1;
        true
    }

    /// Seconds with an optional fraction: `n[.ffffff]S`.
    fn seconds(&mut self) {
        let count = self.bytes[self.at..]
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
        if count == 0 {
            return;
        }
        let Some((micros, end)) = fraction(self.bytes, self.at + count) else {
            return;
        };
        if self.bytes.get(end) != Some(&b'S') {
            return;
        }
        self.add(self.at, self.at + count, MICROS_PER_SECOND);
        self.total += i128::from(micros);
        self.at = end + 1;
        self.components += 1;
    }

    fn add(&mut self, from: usize, to: usize, scale: i128) {
        let digits = &self.bytes[from..to];
        let significant = digits.iter().skip_while(|digit| **digit == b'0').count();
        if significant > MAX_DURATION_COMPONENT_DIGITS {
            self.too_large = true;
            return;
        }
        let value = digits[digits.len() - significant..]
            .iter()
            .fold(0_i128, |total, digit| total * 10 + i128::from(digit - b'0'));
        self.total += value * scale;
    }
}

/// `[-]P[nD][T[nH][nM][n[.ffffff]S]]` as a normalized `timedelta`.
pub(crate) fn parse_duration(text: &str) -> Result<Duration, DurationError> {
    let bytes = text.as_bytes();
    let negative = bytes.first() == Some(&b'-');
    let start = usize::from(negative);
    if bytes.get(start) != Some(&b'P') {
        return Err(DurationError::Shape);
    }
    let mut reader = DurationReader {
        bytes,
        at: start + 1,
        total: 0,
        components: 0,
        too_large: false,
    };
    reader.component(b'D', SECONDS_PER_DAY);
    if bytes.get(reader.at) == Some(&b'T') {
        reader.at += 1;
        let before = reader.components;
        reader.component(b'H', 3600);
        reader.component(b'M', 60);
        reader.seconds();
        if reader.components == before {
            return Err(DurationError::Shape);
        }
    }
    if reader.at != bytes.len() || reader.components == 0 {
        return Err(DurationError::Shape);
    }
    let total = if negative {
        -reader.total
    } else {
        reader.total
    };
    let day = SECONDS_PER_DAY * MICROS_PER_SECOND;
    let (least, most) = (-MAX_DURATION_DAYS * day, (MAX_DURATION_DAYS + 1) * day - 1);
    if reader.too_large || total < least || total > most {
        return Err(DurationError::Range);
    }
    normalized(total).ok_or(DurationError::Range)
}

fn normalized(total: i128) -> Option<Duration> {
    let day = SECONDS_PER_DAY * MICROS_PER_SECOND;
    let rest = total.rem_euclid(day);
    Some(Duration {
        days: i64::try_from(total.div_euclid(day)).ok()?,
        seconds: i64::try_from(rest / MICROS_PER_SECOND).ok()?,
        micros: i64::try_from(rest % MICROS_PER_SECOND).ok()?,
    })
}

/// The canonical ISO 8601 text of a `timedelta`: `PT0S` for zero, otherwise
/// the non-zero components (`-P1DT2H`, `PT0.5S`).
pub(crate) fn format_duration(days: i64, seconds: i64, micros: i64) -> String {
    let day = SECONDS_PER_DAY * MICROS_PER_SECOND;
    let total =
        i128::from(days) * day + i128::from(seconds) * MICROS_PER_SECOND + i128::from(micros);
    if total == 0 {
        return "PT0S".to_owned();
    }
    let size = total.unsigned_abs();
    let day = day.unsigned_abs();
    let micros_per_second = MICROS_PER_SECOND.unsigned_abs();
    let (whole_days, rest) = (size / day, size % day);
    let (hours, rest) = (
        rest / (3600 * micros_per_second),
        rest % (3600 * micros_per_second),
    );
    let (minutes, rest) = (
        rest / (60 * micros_per_second),
        rest % (60 * micros_per_second),
    );
    let (secs, fraction) = (rest / micros_per_second, rest % micros_per_second);
    let mut out = String::from(if total < 0 { "-P" } else { "P" });
    if whole_days > 0 {
        out.push_str(&format!("{whole_days}D"));
    }
    if hours + minutes + secs + fraction > 0 {
        out.push('T');
    }
    for (value, unit) in [(hours, 'H'), (minutes, 'M')] {
        if value > 0 {
            out.push_str(&format!("{value}{unit}"));
        }
    }
    if secs + fraction > 0 {
        out.push_str(&secs.to_string());
        if fraction > 0 {
            let digits = format!("{fraction:06}");
            out.push('.');
            out.push_str(digits.trim_end_matches('0'));
        }
        out.push('S');
    }
    out
}

/// The digit counts of a decimal text: every digit written (the leading `0`
/// of a value below one aside) and the digits after the point.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct DecimalShape {
    pub(crate) digits: usize,
    pub(crate) places: usize,
}

/// A decimal text split at its point, or `None` when it is not
/// `-?(0|[1-9][0-9]*)(\.[0-9]+)?`.
fn decimal_parts(text: &str) -> Option<(bool, &str, &str)> {
    let negative = text.starts_with('-');
    let body = &text[usize::from(negative)..];
    let (whole, fraction) = match body.split_once('.') {
        Some((whole, fraction)) if !fraction.is_empty() => (whole, fraction),
        Some(_) => return None,
        None => (body, ""),
    };
    let leading_zero = whole.len() > 1 && whole.starts_with('0');
    if whole.is_empty() || leading_zero || !all_digits(whole.as_bytes()) {
        return None;
    }
    all_digits(fraction.as_bytes()).then_some((negative, whole, fraction))
}

/// The digit counts of a decimal text, or `None` when it is not one.
pub(crate) fn decimal_shape(text: &str) -> Option<DecimalShape> {
    let (_, whole, fraction) = decimal_parts(text)?;
    let whole_digits = if whole == "0" { 0 } else { whole.len() };
    Some(DecimalShape {
        digits: (whole_digits + fraction.len()).max(1),
        places: fraction.len(),
    })
}

/// A decimal value compared exactly: its digits without leading or trailing
/// zeros (zero has neither and no sign).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DecimalNumber {
    negative: bool,
    whole: String,
    fraction: String,
}

impl DecimalNumber {
    pub(crate) fn parse(text: &str) -> Option<Self> {
        let (negative, whole, fraction) = decimal_parts(text)?;
        let whole = whole.trim_start_matches('0').to_owned();
        let fraction = fraction.trim_end_matches('0').to_owned();
        let zero = whole.is_empty() && fraction.is_empty();
        Some(Self {
            negative: negative && !zero,
            whole,
            fraction,
        })
    }

    /// The digits as an integer scaled by `10^places`; `None` on overflow.
    fn scaled(&self, places: usize) -> Option<u128> {
        let padding = "0".repeat(places - self.fraction.len());
        let digits = format!("{}{}{padding}", self.whole, self.fraction);
        if digits.is_empty() {
            return Some(0);
        }
        digits.parse().ok()
    }

    /// Whether `self` is a whole multiple of `step`; `None` when the scaled
    /// digits exceed 128 bits (the caller defers to Python's exact check).
    pub(crate) fn is_multiple_of(&self, step: &Self) -> Option<bool> {
        let places = self.fraction.len().max(step.fraction.len());
        let (value, step) = (self.scaled(places)?, step.scaled(places)?);
        Some(step != 0 && value % step == 0)
    }

    fn magnitude(&self, other: &Self) -> Ordering {
        self.whole
            .len()
            .cmp(&other.whole.len())
            .then_with(|| self.whole.cmp(&other.whole))
            .then_with(|| self.fraction.cmp(&other.fraction))
    }
}

impl PartialOrd for DecimalNumber {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for DecimalNumber {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self.negative, other.negative) {
            (false, true) => Ordering::Greater,
            (true, false) => Ordering::Less,
            (false, false) => self.magnitude(other),
            (true, true) => other.magnitude(self),
        }
    }
}

/// Standard padded base64, canonical (no stray bits in the last character),
/// with no whitespace; `None` otherwise.
pub(crate) fn decode_base64(text: &str) -> Option<Vec<u8>> {
    STANDARD.decode(text).ok()
}

/// The fixed-point text of a finite `decimal.Decimal` (`format(value, "f")`:
/// `1E+2` is `100`); `ValueError` for NaN, infinities and values with more
/// digits than [`DECIMAL_DIGITS_CEILING`].
fn decimal_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if !value.call_method0("is_finite")?.is_truthy()? {
        return Err(PyValueError::new_err(
            "a decimal that is not finite is not JSON compliant",
        ));
    }
    let adjusted: i64 = value.call_method0("adjusted")?.extract()?;
    let exponent: i64 = value
        .call_method0("as_tuple")?
        .getattr("exponent")?
        .extract()?;
    let whole = u64::try_from(adjusted.max(0)).unwrap_or(u64::MAX);
    let places = u64::try_from((-exponent).max(0)).unwrap_or(u64::MAX);
    let ceiling = u64::try_from(DECIMAL_DIGITS_CEILING).unwrap_or(u64::MAX);
    if whole.saturating_add(1).saturating_add(places) > ceiling {
        return Err(PyValueError::new_err(format!(
            "a decimal with more than {DECIMAL_DIGITS_CEILING} digits is not written as JSON"
        )));
    }
    value.call_method1("__format__", ("f",))?.extract()
}

/// The ISO 8601 text of a `datetime.timedelta`.
fn duration_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let days: i64 = value.getattr("days")?.extract()?;
    let seconds: i64 = value.getattr("seconds")?.extract()?;
    let micros: i64 = value.getattr("microseconds")?.extract()?;
    Ok(format_duration(days, seconds, micros))
}

/// The JSON string a `date`, `time`, `timedelta`, `Decimal` or `bytes` value
/// is written as; `None` for any other value.
pub(crate) fn wire_text(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let py = value.py();
    if let Ok(raw) = value.cast::<PyBytes>() {
        return Ok(Some(STANDARD.encode(raw.as_bytes())));
    }
    if value.is_instance(crate::pytypes::decimal_class(py)?)? {
        return decimal_text(value).map(Some);
    }
    if value.is_instance(crate::pytypes::timedelta_class(py)?)? {
        return duration_text(value).map(Some);
    }
    if value.is_instance(crate::pytypes::time_class(py)?)?
        || value.is_instance(crate::pytypes::date_class(py)?)?
    {
        return value.call_method0("isoformat")?.extract().map(Some);
    }
    Ok(None)
}

/// `core.wire_text(value)`: the JSON string of a `date`, `time`, `timedelta`,
/// `Decimal` or `bytes` value, `None` for any other value.
#[pyfunction(name = "wire_text")]
pub fn wire_text_of(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    wire_text(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_are_calendar_dates_in_four_digit_years() {
        let day = parse_date("2024-02-29").unwrap();
        assert_eq!(
            (day.year(), u8::from(day.month()), day.day()),
            (2024, 2, 29)
        );
        assert!(parse_date("0001-01-01").is_some());
        assert!(parse_date("9999-12-31").is_some());
        for bad in [
            "2026-02-29",
            "0000-01-01",
            "2026-13-01",
            "2026-00-01",
            "2026-1-01",
            "2026-01-01T",
            "２０２６-01-01",
            "+026-01-01",
            "",
        ] {
            assert_eq!(parse_date(bad), None, "{bad}");
        }
    }

    #[test]
    fn times_keep_their_offset() {
        let at = parse_time("23:59:59.5-05:30").unwrap();
        assert_eq!(
            (at.hour, at.minute, at.second, at.micro),
            (23, 59, 59, 500_000)
        );
        assert_eq!(at.offset, Some(-(5 * 3600 + 30 * 60)));
        assert_eq!(parse_time("00:00:00").unwrap().offset, None);
        assert_eq!(parse_time("00:00:00Z").unwrap().offset, Some(0));
        for bad in [
            "24:00:00Z",
            "00:60:00Z",
            "00:00:60Z",
            "00:00Z",
            "00:00:00.Z",
            "00:00:00.1234567Z",
            "00:00:00z",
            "00:00:00+24:00",
            "00:00:00+0100",
            "00:00:00 ",
            "-1:00:00Z",
        ] {
            assert_eq!(parse_time(bad), None, "{bad}");
        }
    }

    #[test]
    fn durations_follow_python_normalization() {
        let parsed = parse_duration("-PT1S").unwrap();
        assert_eq!(
            (parsed.days, parsed.seconds, parsed.micros),
            (-1, 86_399, 0)
        );
        let parsed = parse_duration("P1DT2H3M4.000005S").unwrap();
        assert_eq!((parsed.days, parsed.seconds, parsed.micros), (1, 7384, 5));
        assert!(parse_duration("P999999999DT23H59M59.999999S").is_ok());
        assert!(parse_duration("-P999999999D").is_ok());
        for huge in [
            "P1000000000D",
            "-P999999999DT0.000001S",
            "PT99999999999999999999H",
        ] {
            assert_eq!(parse_duration(huge), Err(DurationError::Range), "{huge}");
        }
        for bad in [
            "P",
            "PT",
            "-P",
            "P1DT",
            "P1Y",
            "P1W",
            "PT1.5H",
            "PT1S1M",
            "p1d",
            "P1D ",
            "PT0.1234567S",
            "+P1D",
            "PT99999999999999999999H1X",
        ] {
            assert_eq!(parse_duration(bad), Err(DurationError::Shape), "{bad}");
        }
    }

    #[test]
    fn durations_are_written_canonically() {
        assert_eq!(format_duration(0, 0, 0), "PT0S");
        assert_eq!(format_duration(-1, 86_399, 0), "-PT1S");
        assert_eq!(format_duration(1, 7384, 5), "P1DT2H3M4.000005S");
        assert_eq!(format_duration(0, 0, 500_000), "PT0.5S");
        assert_eq!(format_duration(2, 0, 0), "P2D");
        assert_eq!(format_duration(-999_999_999, 0, 0), "-P999999999D");
    }

    #[test]
    fn decimals_count_written_digits() {
        let shape = |text| decimal_shape(text).map(|s| (s.digits, s.places));
        assert_eq!(shape("0"), Some((1, 0)));
        assert_eq!(shape("-0.05"), Some((2, 2)));
        assert_eq!(shape("10.50"), Some((4, 2)));
        assert_eq!(shape("0.000"), Some((3, 3)));
        for bad in [
            "01", "1.", ".5", "+1", "1e5", "NaN", "1_0", " 1", "", "-", "１",
        ] {
            assert_eq!(decimal_shape(bad), None, "{bad}");
        }
    }

    #[test]
    fn decimals_compare_exactly() {
        let number = |text| DecimalNumber::parse(text).unwrap();
        assert_eq!(number("1.50").cmp(&number("1.5")), Ordering::Equal);
        assert_eq!(number("-0").cmp(&number("0.000")), Ordering::Equal);
        assert_eq!(number("0.05").cmp(&number("0.5")), Ordering::Less);
        assert_eq!(number("-1.5").cmp(&number("-1.49")), Ordering::Less);
        assert_eq!(number("10").cmp(&number("9.99")), Ordering::Greater);
        assert_eq!(number("-10").cmp(&number("1")), Ordering::Less);
    }

    #[test]
    fn base64_is_strict() {
        assert_eq!(decode_base64("QUI="), Some(b"AB".to_vec()));
        assert_eq!(decode_base64(""), Some(Vec::new()));
        for bad in ["QUI", "QR==", "QU I=", "-_-_", "QUI=\n", "QQ==="] {
            assert_eq!(decode_base64(bad), None, "{bad}");
        }
    }
}
