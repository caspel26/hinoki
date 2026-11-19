//! PostgreSQL `numeric` wire format, converted from and to decimal text.
//!
//! The binary form is a header (digit count, weight, sign, display scale)
//! followed by base-10000 digits. Hinoki carries a `Decimal` as its text, so the
//! codec needs no numeric type: it only moves digits and keeps the scale.

const BASE_DIGITS: usize = 4;
const SIGN_NEGATIVE: u16 = 0x4000;
const MAX_TEXT: usize = 140_000;
const MAX_SCALE: i64 = 16_383;
const MAX_EXPONENT: i64 = 200_000;

/// Why a decimal text or a numeric value could not be converted.
#[derive(Debug, PartialEq, Eq)]
pub struct NumericError(pub &'static str);

struct Parsed {
    negative: bool,
    digits: String,
    /// Number of digits of `digits` that sit before the decimal point (may be
    /// negative or larger than the digit count).
    point: i64,
    scale: i64,
}

fn split_exponent(text: &str) -> Result<(&str, i64), NumericError> {
    let Some(at) = text.find(['e', 'E']) else {
        return Ok((text, 0));
    };
    let exponent = text[at + 1..]
        .parse::<i64>()
        .map_err(|_| NumericError("invalid decimal exponent"))?;
    if exponent.abs() > MAX_EXPONENT {
        return Err(NumericError("decimal exponent out of range"));
    }
    Ok((&text[..at], exponent))
}

fn parse(text: &str) -> Result<Parsed, NumericError> {
    if text.is_empty() || text.len() > MAX_TEXT {
        return Err(NumericError("invalid decimal text"));
    }
    let (mantissa, exponent) = split_exponent(text)?;
    let (negative, body) = match mantissa.as_bytes().first() {
        Some(b'-') => (true, &mantissa[1..]),
        Some(b'+') => (false, &mantissa[1..]),
        _ => (false, mantissa),
    };
    let (whole, fraction) = body.split_once('.').unwrap_or((body, ""));
    let valid = |part: &str| part.bytes().all(|b| b.is_ascii_digit());
    if !valid(whole) || !valid(fraction) || (whole.is_empty() && fraction.is_empty()) {
        return Err(NumericError("invalid decimal text"));
    }
    let scale = (fraction.len() as i64 - exponent).max(0);
    if scale > MAX_SCALE {
        return Err(NumericError("decimal scale out of range"));
    }
    Ok(Parsed {
        negative,
        digits: format!("{whole}{fraction}"),
        point: whole.len() as i64 + exponent,
        scale,
    })
}

/// The integer and fraction digit strings of a parsed value.
fn align(parsed: &Parsed) -> (String, String) {
    let digits = parsed.digits.as_str();
    let length = digits.len() as i64;
    if parsed.point <= 0 {
        let zeros = "0".repeat((-parsed.point) as usize);
        (String::new(), format!("{zeros}{digits}"))
    } else if parsed.point >= length {
        let zeros = "0".repeat((parsed.point - length) as usize);
        (format!("{digits}{zeros}"), String::new())
    } else {
        let (whole, fraction) = digits.split_at(parsed.point as usize);
        (whole.to_owned(), fraction.to_owned())
    }
}

fn groups(text: &str, pad_left: bool) -> Vec<i16> {
    let missing = (BASE_DIGITS - text.len() % BASE_DIGITS) % BASE_DIGITS;
    let padded = if pad_left {
        format!("{}{text}", "0".repeat(missing))
    } else {
        format!("{text}{}", "0".repeat(missing))
    };
    padded
        .as_bytes()
        .chunks(BASE_DIGITS)
        .map(|chunk| chunk.iter().fold(0i16, |n, d| n * 10 + i16::from(d - b'0')))
        .collect()
}

/// Encode decimal text (as Python's `str(Decimal)` writes it) as `numeric`.
pub fn encode(text: &str, out: &mut Vec<u8>) -> Result<(), NumericError> {
    let parsed = parse(text)?;
    let (whole, fraction) = align(&parsed);
    let whole = whole.trim_start_matches('0');
    let mut digits = groups(whole, true);
    let mut weight = digits.len() as i64 - 1;
    digits.extend(groups(&fraction, false));
    let leading = digits.iter().take_while(|d| **d == 0).count();
    digits.drain(..leading);
    weight -= leading as i64;
    while digits.last() == Some(&0) {
        digits.pop();
    }
    if digits.is_empty() {
        weight = 0;
    }
    let weight = i16::try_from(weight).map_err(|_| NumericError("decimal out of range"))?;
    let sign = if parsed.negative && !digits.is_empty() {
        SIGN_NEGATIVE
    } else {
        0
    };
    out.extend((digits.len() as i16).to_be_bytes());
    out.extend(weight.to_be_bytes());
    out.extend(sign.to_be_bytes());
    out.extend((parsed.scale as u16).to_be_bytes());
    for digit in digits {
        out.extend(digit.to_be_bytes());
    }
    Ok(())
}

fn word(raw: &[u8], at: usize) -> Result<[u8; 2], NumericError> {
    raw.get(at..at + 2)
        .and_then(|pair| pair.try_into().ok())
        .ok_or(NumericError("truncated numeric value"))
}

/// Decode a `numeric` value to decimal text that keeps its display scale.
pub fn decode(raw: &[u8]) -> Result<String, NumericError> {
    let count = u16::from_be_bytes(word(raw, 0)?) as usize;
    let weight = i64::from(i16::from_be_bytes(word(raw, 2)?));
    let sign = u16::from_be_bytes(word(raw, 4)?);
    let scale = u16::from_be_bytes(word(raw, 6)?) as usize;
    if sign != 0 && sign != SIGN_NEGATIVE {
        return Err(NumericError("NaN and infinity are not decimals"));
    }
    let mut digits = Vec::with_capacity(count);
    for index in 0..count {
        digits.push(i64::from(i16::from_be_bytes(word(raw, 8 + index * 2)?)));
    }
    let group_at = |position: i64| -> i64 {
        usize::try_from(weight - position)
            .ok()
            .and_then(|index| digits.get(index))
            .copied()
            .unwrap_or(0)
    };
    let mut whole = String::new();
    for position in (0..=weight.max(0)).rev() {
        whole.push_str(&format!("{:04}", group_at(position)));
    }
    let whole = whole.trim_start_matches('0');
    let mut text = if whole.is_empty() { "0" } else { whole }.to_owned();
    if scale > 0 {
        let mut fraction = String::new();
        let mut position = -1;
        while fraction.len() < scale {
            fraction.push_str(&format!("{:04}", group_at(position)));
            position -= 1;
        }
        fraction.truncate(scale);
        text.push('.');
        text.push_str(&fraction);
    }
    let zero = digits.iter().all(|d| *d == 0);
    Ok(if sign == SIGN_NEGATIVE && !zero {
        format!("-{text}")
    } else {
        text
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bytes(text: &str) -> Vec<u8> {
        let mut out = Vec::new();
        encode(text, &mut out).unwrap();
        out
    }

    fn header(raw: &[u8]) -> (i16, i16, u16, u16, Vec<i16>) {
        let n = |at: usize| i16::from_be_bytes([raw[at], raw[at + 1]]);
        let digits = raw[8..]
            .chunks(2)
            .map(|c| i16::from_be_bytes([c[0], c[1]]))
            .collect();
        (n(0), n(2), n(4) as u16, n(6) as u16, digits)
    }

    #[test]
    fn encodes_the_documented_layouts() {
        assert_eq!(header(&bytes("12.3")), (2, 0, 0, 1, vec![12, 3000]));
        assert_eq!(header(&bytes("0.00001")), (1, -2, 0, 5, vec![1000]));
        assert_eq!(header(&bytes("100000000")), (1, 2, 0, 0, vec![1]));
        assert_eq!(header(&bytes("-1")), (1, 0, SIGN_NEGATIVE, 0, vec![1]));
        assert_eq!(header(&bytes("0.00")), (0, 0, 0, 2, vec![]));
        assert_eq!(header(&bytes("-0")), (0, 0, 0, 0, vec![]));
        assert_eq!(header(&bytes("1E+2")), (1, 0, 0, 0, vec![100]));
        assert_eq!(header(&bytes("1.5E-7")), (1, -2, 0, 8, vec![15]));
    }

    #[test]
    fn round_trips_and_keeps_the_scale() {
        for text in [
            "0",
            "0.00",
            "12.30",
            "-12.30",
            "0.0001",
            "123456789.123456789",
            "-0.5",
            "99999999999999999999999999999999999999",
            "1000000",
            "0.000000000001",
        ] {
            assert_eq!(decode(&bytes(text)).unwrap(), text, "{text}");
        }
        assert_eq!(decode(&bytes("1E+2")).unwrap(), "100");
        assert_eq!(decode(&bytes("1.5E-7")).unwrap(), "0.00000015");
        assert_eq!(decode(&bytes("-0")).unwrap(), "0");
    }

    #[test]
    fn rejects_text_that_is_not_a_finite_decimal() {
        for text in [
            "", "NaN", "Infinity", "-Inf", "1.2.3", "1e", "abc", ".", "1e999999",
        ] {
            let mut out = Vec::new();
            assert!(encode(text, &mut out).is_err(), "{text}");
        }
    }

    #[test]
    fn refuses_nan_infinity_and_truncated_values() {
        let nan = [0, 0, 0, 0, 0xC0, 0, 0, 0];
        assert!(decode(&nan).is_err());
        assert!(decode(&[0, 1, 0, 0, 0, 0, 0, 0]).is_err());
        assert!(decode(&[0, 0]).is_err());
    }
}
