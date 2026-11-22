//! Keyset cursors: opaque, signed position tokens and the page assembly around
//! them.
//!
//! A token carries only the sort-key values of one row and a direction. It
//! never carries filters or scope: the keyset condition is ANDed onto the
//! caller's own query, so a replayed or borrowed token cannot show a row the
//! query would not. The token is `base64url(payload || HMAC-SHA256)`; the
//! MAC covers a label, the fingerprint of the ordering it was issued for and
//! the payload, so a token issued for another route, table or ordering fails
//! like a forged one.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use hinoki_orm::Value;
use hmac::{Hmac, KeyInit, Mac};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyList;
use sha2::Sha256;
use time::OffsetDateTime;
use uuid::Uuid;

use crate::db::{from_value, to_value};
use crate::rows::{encode_cursor_page, RowPlan};

type Row = Vec<(String, Value)>;

const VERSION: u8 = 1;
const LABEL: &[u8] = b"hinoki.cursor.v1";
const MAC_LEN: usize = 32;
const HEADER_LEN: usize = 7;
const MIN_SECRET: usize = 32;
const MAX_KEYS: usize = 8;
const MAX_TOKEN_CHARS: usize = 1024;
const MAX_TEXT_BYTES: usize = 512;
const CLOCK_SKEW_SECONDS: u64 = 60;

const TAG_INT: u8 = 1;
const TAG_TEXT: u8 = 2;
const TAG_UUID: u8 = 3;
const TAG_TIMESTAMP: u8 = 4;
const TAG_FLOAT: u8 = 5;
const TAG_BOOL: u8 = 6;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum CursorError {
    Malformed,
    Expired,
    /// A sort key that cannot be put in a token (NULL, too long, not finite).
    Unencodable,
}

impl CursorError {
    fn message(&self) -> &'static str {
        match self {
            Self::Malformed => "invalid",
            Self::Expired => "expired",
            Self::Unencodable => "unencodable",
        }
    }
}

/// Where a page sits relative to the cursor that asked for it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mode {
    First,
    Forward,
    Backward,
}

impl Mode {
    fn parse(code: u8) -> PyResult<Self> {
        match code {
            0 => Ok(Self::First),
            1 => Ok(Self::Forward),
            2 => Ok(Self::Backward),
            _ => Err(PyValueError::new_err("unknown cursor mode")),
        }
    }
}

pub(crate) struct Codec {
    secret: Vec<u8>,
    fingerprint: Vec<u8>,
    ttl: Option<u64>,
}

fn now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

impl Codec {
    fn mac(&self, payload: &[u8]) -> Hmac<Sha256> {
        let mut mac = <Hmac<Sha256> as KeyInit>::new_from_slice(&self.secret)
            .expect("HMAC accepts a key of any length");
        mac.update(LABEL);
        mac.update(&(self.fingerprint.len() as u32).to_be_bytes());
        mac.update(&self.fingerprint);
        mac.update(payload);
        mac
    }

    pub(crate) fn encode(&self, backward: bool, values: &[Value]) -> Result<String, CursorError> {
        self.encode_at(backward, values, now_seconds())
    }

    fn encode_at(&self, backward: bool, values: &[Value], now: u64) -> Result<String, CursorError> {
        if values.is_empty() || values.len() > MAX_KEYS {
            return Err(CursorError::Unencodable);
        }
        let mut payload = Vec::with_capacity(HEADER_LEN + 9 * values.len() + MAC_LEN);
        payload.push(VERSION);
        payload.push(u8::from(backward));
        payload.extend_from_slice(&u32::try_from(now).unwrap_or(u32::MAX).to_be_bytes());
        payload.push(values.len() as u8);
        for value in values {
            write_value(&mut payload, value)?;
        }
        let tag = self.mac(&payload).finalize().into_bytes();
        payload.extend_from_slice(&tag);
        Ok(URL_SAFE_NO_PAD.encode(payload))
    }

    pub(crate) fn decode(
        &self,
        token: &str,
        expected: usize,
    ) -> Result<(bool, Vec<Value>), CursorError> {
        self.decode_at(token, expected, now_seconds())
    }

    fn decode_at(
        &self,
        token: &str,
        expected: usize,
        now: u64,
    ) -> Result<(bool, Vec<Value>), CursorError> {
        if token.is_empty() || token.len() > MAX_TOKEN_CHARS {
            return Err(CursorError::Malformed);
        }
        let bytes = URL_SAFE_NO_PAD
            .decode(token)
            .map_err(|_| CursorError::Malformed)?;
        if bytes.len() < HEADER_LEN + MAC_LEN {
            return Err(CursorError::Malformed);
        }
        let (payload, tag) = bytes.split_at(bytes.len() - MAC_LEN);
        self.mac(payload)
            .verify_slice(tag)
            .map_err(|_| CursorError::Malformed)?;
        let [version, flags, a, b, c, d, count] = payload[..HEADER_LEN] else {
            return Err(CursorError::Malformed);
        };
        let issued = u64::from(u32::from_be_bytes([a, b, c, d]));
        if version != VERSION || flags > 1 || usize::from(count) != expected {
            return Err(CursorError::Malformed);
        }
        self.check_age(issued, now)?;
        let mut rest = &payload[HEADER_LEN..];
        let mut values = Vec::with_capacity(expected);
        for _ in 0..expected {
            values.push(read_value(&mut rest)?);
        }
        if !rest.is_empty() {
            return Err(CursorError::Malformed);
        }
        Ok((flags == 1, values))
    }

    fn check_age(&self, issued: u64, now: u64) -> Result<(), CursorError> {
        if issued > now + CLOCK_SKEW_SECONDS {
            return Err(CursorError::Malformed);
        }
        match self.ttl {
            Some(ttl) if now.saturating_sub(issued) > ttl => Err(CursorError::Expired),
            _ => Ok(()),
        }
    }
}

fn write_value(out: &mut Vec<u8>, value: &Value) -> Result<(), CursorError> {
    match value {
        Value::Int(number) => {
            out.push(TAG_INT);
            out.extend_from_slice(&number.to_be_bytes());
        }
        Value::Text(text) => {
            let length = u16::try_from(text.len())
                .ok()
                .filter(|length| usize::from(*length) <= MAX_TEXT_BYTES)
                .ok_or(CursorError::Unencodable)?;
            out.push(TAG_TEXT);
            out.extend_from_slice(&length.to_be_bytes());
            out.extend_from_slice(text.as_bytes());
        }
        Value::Uuid(uuid) => {
            out.push(TAG_UUID);
            out.extend_from_slice(uuid.as_bytes());
        }
        Value::Timestamp(moment) => {
            let micros = i64::try_from(moment.unix_timestamp_nanos() / 1000)
                .map_err(|_| CursorError::Unencodable)?;
            out.push(TAG_TIMESTAMP);
            out.extend_from_slice(&micros.to_be_bytes());
        }
        Value::Float(number) if number.is_finite() => {
            out.push(TAG_FLOAT);
            out.extend_from_slice(&number.to_bits().to_be_bytes());
        }
        Value::Bool(flag) => {
            out.push(TAG_BOOL);
            out.push(u8::from(*flag));
        }
        _ => return Err(CursorError::Unencodable),
    }
    Ok(())
}

fn take<'a>(rest: &mut &'a [u8], count: usize) -> Result<&'a [u8], CursorError> {
    if rest.len() < count {
        return Err(CursorError::Malformed);
    }
    let (head, tail) = rest.split_at(count);
    *rest = tail;
    Ok(head)
}

fn take_array<const N: usize>(rest: &mut &[u8]) -> Result<[u8; N], CursorError> {
    take(rest, N)?
        .try_into()
        .map_err(|_| CursorError::Malformed)
}

fn read_value(rest: &mut &[u8]) -> Result<Value, CursorError> {
    let [tag] = take_array::<1>(rest)?;
    match tag {
        TAG_INT => Ok(Value::Int(i64::from_be_bytes(take_array(rest)?))),
        TAG_TEXT => {
            let length = usize::from(u16::from_be_bytes(take_array(rest)?));
            if length > MAX_TEXT_BYTES {
                return Err(CursorError::Malformed);
            }
            let text =
                std::str::from_utf8(take(rest, length)?).map_err(|_| CursorError::Malformed)?;
            Ok(Value::Text(text.to_owned()))
        }
        TAG_UUID => Ok(Value::Uuid(Uuid::from_bytes(take_array(rest)?))),
        TAG_TIMESTAMP => {
            let micros = i64::from_be_bytes(take_array(rest)?);
            OffsetDateTime::from_unix_timestamp_nanos(i128::from(micros) * 1000)
                .map(Value::timestamp)
                .map_err(|_| CursorError::Malformed)
        }
        TAG_FLOAT => {
            let number = f64::from_bits(u64::from_be_bytes(take_array(rest)?));
            if number.is_finite() {
                Ok(Value::Float(number))
            } else {
                Err(CursorError::Malformed)
            }
        }
        TAG_BOOL => match take_array::<1>(rest)? {
            [0] => Ok(Value::Bool(false)),
            [1] => Ok(Value::Bool(true)),
            _ => Err(CursorError::Malformed),
        },
        _ => Err(CursorError::Malformed),
    }
}

/// The values of `keys` in `row`, in key order.
fn key_values(row: &Row, keys: &[String]) -> Result<Vec<Value>, CursorError> {
    keys.iter()
        .map(|key| {
            row.iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value.clone())
                .ok_or(CursorError::Unencodable)
        })
        .collect()
}

pub(crate) struct Links {
    pub(crate) next: Option<String>,
    pub(crate) previous: Option<String>,
}

impl Codec {
    fn links(
        &self,
        rows: &[Row],
        keys: &[String],
        mode: Mode,
        has_more: bool,
    ) -> Result<Links, CursorError> {
        let after_last = |codec: &Self| match rows.last() {
            Some(row) => codec.encode(false, &key_values(row, keys)?).map(Some),
            None => Ok(None),
        };
        let before_first = |codec: &Self| match rows.first() {
            Some(row) => codec.encode(true, &key_values(row, keys)?).map(Some),
            None => Ok(None),
        };
        Ok(match mode {
            Mode::First => Links {
                next: if has_more { after_last(self)? } else { None },
                previous: None,
            },
            Mode::Forward => Links {
                next: if has_more { after_last(self)? } else { None },
                previous: before_first(self)?,
            },
            Mode::Backward => Links {
                next: after_last(self)?,
                previous: if has_more { before_first(self)? } else { None },
            },
        })
    }
}

/// What the page needs besides its rows.
pub(crate) struct CursorQuery {
    pub(crate) codec: Arc<Codec>,
    pub(crate) keys: Vec<String>,
    pub(crate) limit: usize,
    pub(crate) mode: Mode,
}

pub(crate) enum CursorWire {
    Encoded(Vec<u8>),
    /// The rows in page order, cut to the limit, and whether more exist.
    Fallback(Vec<Row>, bool),
}

impl CursorQuery {
    /// The rows were read in walking order with one extra row: cut it, put a
    /// backward page in display order and encode the page.
    pub(crate) fn finish(&self, mut rows: Vec<Row>, plan: &RowPlan) -> CursorWire {
        let has_more = rows.len() > self.limit;
        rows.truncate(self.limit);
        if self.mode == Mode::Backward {
            rows.reverse();
        }
        let encoded = self
            .codec
            .links(&rows, &self.keys, self.mode, has_more)
            .ok()
            .and_then(|links| encode_cursor_page(&rows, plan, &links, self.limit as u64));
        match encoded {
            Some(bytes) => CursorWire::Encoded(bytes),
            None => CursorWire::Fallback(rows, has_more),
        }
    }
}

#[pyclass(name = "_CursorCodec", module = "hinoki.core", frozen)]
pub(crate) struct PyCursorCodec {
    pub(crate) inner: Arc<Codec>,
}

fn error(err: CursorError) -> PyErr {
    PyValueError::new_err(err.message())
}

#[pymethods]
impl PyCursorCodec {
    #[new]
    #[pyo3(signature = (secret, fingerprint, ttl=None))]
    fn new(secret: &[u8], fingerprint: &[u8], ttl: Option<u64>) -> PyResult<Self> {
        if secret.len() < MIN_SECRET {
            return Err(PyValueError::new_err(format!(
                "a cursor secret needs at least {MIN_SECRET} bytes"
            )));
        }
        Ok(Self {
            inner: Arc::new(Codec {
                secret: secret.to_vec(),
                fingerprint: fingerprint.to_vec(),
                ttl,
            }),
        })
    }

    /// `(backward, values)` of a token; `ValueError("invalid" | "expired")`
    /// for anything that is not a fresh token issued for this ordering.
    fn decode(
        &self,
        py: Python<'_>,
        token: &str,
        count: usize,
    ) -> PyResult<(bool, Vec<Py<PyAny>>)> {
        let (backward, values) = self.inner.decode(token, count).map_err(error)?;
        let values = values
            .iter()
            .map(|value| from_value(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        Ok((backward, values))
    }

    fn encode(&self, backward: bool, values: &Bound<'_, PyList>) -> PyResult<String> {
        let values = values
            .iter()
            .map(|value| to_value(&value))
            .collect::<PyResult<Vec<_>>>()?;
        self.inner.encode(backward, &values).map_err(error)
    }
}

impl CursorQuery {
    pub(crate) fn from_python(
        codec: &PyCursorCodec,
        keys: Vec<String>,
        limit: u64,
        mode: u8,
    ) -> PyResult<Self> {
        Ok(Self {
            codec: codec.inner.clone(),
            keys,
            limit: usize::try_from(limit)
                .map_err(|_| PyValueError::new_err("cursor limit out of range"))?,
            mode: Mode::parse(mode)?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn codec(ttl: Option<u64>, fingerprint: &[u8]) -> Codec {
        Codec {
            secret: vec![7; 32],
            fingerprint: fingerprint.to_vec(),
            ttl,
        }
    }

    fn sample() -> Vec<Value> {
        vec![
            Value::Text("héllo".into()),
            Value::Timestamp(time::macros::datetime!(2026-11-22 1:02:03.456789 UTC)),
            Value::Int(-42),
            Value::Uuid(Uuid::from_u128(9)),
            Value::Float(1.5),
            Value::Bool(true),
        ]
    }

    #[test]
    fn a_token_round_trips_every_key_type_and_direction() {
        let codec = codec(None, b"posts|id");
        let token = codec.encode_at(true, &sample(), 1000).unwrap();
        assert!(token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
        let (backward, values) = codec.decode_at(&token, 6, 1000).unwrap();
        assert!(backward);
        assert_eq!(values, sample());
    }

    #[test]
    fn any_changed_character_is_rejected() {
        let codec = codec(None, b"posts|id");
        let token = codec.encode_at(false, &sample()[2..3], 1000).unwrap();
        for index in 0..token.len() {
            let mut bytes = token.clone().into_bytes();
            bytes[index] = if bytes[index] == b'A' { b'B' } else { b'A' };
            let changed = String::from_utf8(bytes).unwrap();
            assert_eq!(
                codec.decode_at(&changed, 1, 1000).unwrap_err(),
                CursorError::Malformed,
                "{changed}"
            );
        }
    }

    #[test]
    fn another_ordering_secret_or_key_count_is_rejected() {
        let issuer = codec(None, b"posts|id");
        let token = issuer.encode_at(false, &[Value::Int(1)], 1000).unwrap();
        assert!(codec(None, b"posts|-id")
            .decode_at(&token, 1, 1000)
            .is_err());
        let mut other = codec(None, b"posts|id");
        other.secret = vec![8; 32];
        assert!(other.decode_at(&token, 1, 1000).is_err());
        assert!(issuer.decode_at(&token, 2, 1000).is_err());
    }

    #[test]
    fn garbage_truncation_and_oversize_are_malformed_not_panics() {
        let codec = codec(None, b"f");
        let token = codec.encode_at(false, &[Value::Int(1)], 1000).unwrap();
        for bad in ["", "!", "AAAA", &token[..token.len() - 1], &token[1..]] {
            assert_eq!(
                codec.decode_at(bad, 1, 1000).unwrap_err(),
                CursorError::Malformed
            );
        }
        assert!(codec
            .decode_at(&"A".repeat(MAX_TOKEN_CHARS + 1), 1, 1000)
            .is_err());
        assert!(codec.decode_at(&format!("{token}="), 1, 1000).is_err());
    }

    #[test]
    fn a_token_expires_after_its_ttl_and_future_tokens_are_refused() {
        let codec = codec(Some(60), b"f");
        let token = codec.encode_at(false, &[Value::Int(1)], 1000).unwrap();
        assert!(codec.decode_at(&token, 1, 1060).is_ok());
        assert_eq!(
            codec.decode_at(&token, 1, 1061).unwrap_err(),
            CursorError::Expired
        );
        assert!(codec
            .decode_at(&token, 1, 1000 - CLOCK_SKEW_SECONDS)
            .is_ok());
        assert_eq!(
            codec
                .decode_at(&token, 1, 1000 - CLOCK_SKEW_SECONDS - 1)
                .unwrap_err(),
            CursorError::Malformed
        );
        let forever = self::codec(None, b"f");
        let old = forever.encode_at(false, &[Value::Int(1)], 1).unwrap();
        assert!(forever.decode_at(&old, 1, 10_000_000).is_ok());
    }

    #[test]
    fn keys_that_cannot_be_ordered_stably_are_not_encoded() {
        let codec = codec(None, b"f");
        for value in [
            Value::Null,
            Value::Float(f64::NAN),
            Value::Float(f64::INFINITY),
            Value::Bytes(vec![1]),
            Value::Text("x".repeat(MAX_TEXT_BYTES + 1)),
        ] {
            assert_eq!(
                codec.encode_at(false, &[value], 1).unwrap_err(),
                CursorError::Unencodable
            );
        }
        assert!(codec.encode_at(false, &[], 1).is_err());
        assert!(codec
            .encode_at(false, &[Value::Text("x".repeat(MAX_TEXT_BYTES))], 1)
            .is_ok());
    }

    fn rows(ids: &[i64]) -> Vec<Row> {
        ids.iter()
            .map(|id| vec![("id".to_owned(), Value::Int(*id))])
            .collect()
    }

    fn decoded(codec: &Codec, token: &Option<String>) -> (bool, i64) {
        let (backward, values) = codec.decode(token.as_ref().unwrap(), 1).unwrap();
        let Value::Int(id) = values[0] else { panic!() };
        (backward, id)
    }

    #[test]
    fn links_follow_the_mode_and_whether_more_rows_exist() {
        let codec = codec(None, b"f");
        let keys = vec!["id".to_owned()];
        let page = rows(&[3, 4]);
        let first = codec.links(&page, &keys, Mode::First, true).unwrap();
        assert!(first.previous.is_none());
        assert_eq!(decoded(&codec, &first.next), (false, 4));
        let last = codec.links(&page, &keys, Mode::First, false).unwrap();
        assert!(last.next.is_none() && last.previous.is_none());
        let forward = codec.links(&page, &keys, Mode::Forward, false).unwrap();
        assert!(forward.next.is_none());
        assert_eq!(decoded(&codec, &forward.previous), (true, 3));
        let back = codec.links(&page, &keys, Mode::Backward, true).unwrap();
        assert_eq!(decoded(&codec, &back.next), (false, 4));
        assert_eq!(decoded(&codec, &back.previous), (true, 3));
        let start = codec.links(&page, &keys, Mode::Backward, false).unwrap();
        assert!(start.previous.is_none() && start.next.is_some());
        let empty = codec.links(&[], &keys, Mode::Forward, false).unwrap();
        assert!(empty.next.is_none() && empty.previous.is_none());
    }
}
