//! JSON Web Tokens (compact JWS) with a fixed algorithm allow-list.
//!
//! The token's `alg` is only *compared* with the algorithm configured for the
//! key; it never selects a verifier. Supported: `HS256`, `HS384`, `HS512` and
//! `EdDSA` (Ed25519). `none`, `crit` and `zip` are refused. Claims `exp`, `nbf`,
//! `iat`, `iss` and `aud` are checked here with a caller-supplied clock.
//! Failures carry a short internal reason; callers must not show it to clients.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use hmac::{Hmac, KeyInit, Mac};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use serde::de::{Deserializer, MapAccess, Visitor};
use serde_json::{Map, Value};
use sha2::{Sha256, Sha384, Sha512};

const MAX_TOKEN: usize = 8192;
const MAX_LEEWAY: i64 = 120;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Alg {
    Hs256,
    Hs384,
    Hs512,
    EdDsa,
}

impl Alg {
    pub fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "HS256" => Self::Hs256,
            "HS384" => Self::Hs384,
            "HS512" => Self::Hs512,
            "EdDSA" => Self::EdDsa,
            _ => return None,
        })
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Hs256 => "HS256",
            Self::Hs384 => "HS384",
            Self::Hs512 => "HS512",
            Self::EdDsa => "EdDSA",
        }
    }

    /// Shortest HMAC secret, the hash output size (RFC 7518 3.2); `None` for
    /// `EdDSA`, whose key length the signature itself checks.
    pub fn min_secret(self) -> Option<usize> {
        match self {
            Self::Hs256 => Some(32),
            Self::Hs384 => Some(48),
            Self::Hs512 => Some(64),
            Self::EdDsa => None,
        }
    }
}

/// Key material. For `EdDSA`, `public` marks verify-only keys; otherwise the
/// bytes are the 32-byte private seed.
pub struct Key {
    pub kid: Option<String>,
    pub alg: Alg,
    pub bytes: Vec<u8>,
    pub public: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Fail {
    Malformed,
    Algorithm,
    Key,
    Signature,
    Expired,
    NotYetValid,
    Issuer,
    Audience,
    Claims,
}

impl Fail {
    pub fn reason(&self) -> &'static str {
        match self {
            Self::Malformed => "malformed",
            Self::Algorithm => "algorithm",
            Self::Key => "key",
            Self::Signature => "signature",
            Self::Expired => "expired",
            Self::NotYetValid => "not_yet_valid",
            Self::Issuer => "issuer",
            Self::Audience => "audience",
            Self::Claims => "claims",
        }
    }
}

/// Any JSON value, refusing duplicate object keys at every depth (an ambiguous
/// `exp` or `alg` is an attack surface, not a feature).
struct Strict(Value);

impl<'de> serde::Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Walk;
        impl<'de> Visitor<'de> for Walk {
            type Value = Strict;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON value")
            }
            fn visit_bool<E>(self, v: bool) -> Result<Strict, E> {
                Ok(Strict(Value::Bool(v)))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Strict, E> {
                Ok(Strict(Value::from(v)))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Strict, E> {
                Ok(Strict(Value::from(v)))
            }
            fn visit_f64<E>(self, v: f64) -> Result<Strict, E> {
                Ok(Strict(Value::from(v)))
            }
            fn visit_str<E>(self, v: &str) -> Result<Strict, E> {
                Ok(Strict(Value::String(v.to_owned())))
            }
            fn visit_unit<E>(self) -> Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> Result<Strict, A::Error> {
                let mut out = Vec::new();
                while let Some(Strict(item)) = seq.next_element()? {
                    out.push(item);
                }
                Ok(Strict(Value::Array(out)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Strict, A::Error> {
                let mut out = Map::new();
                while let Some((key, Strict(value))) = map.next_entry::<String, Strict>()? {
                    if out.insert(key, value).is_some() {
                        return Err(serde::de::Error::custom("duplicate key"));
                    }
                }
                Ok(Strict(Value::Object(out)))
            }
        }
        deserializer.deserialize_any(Walk)
    }
}

/// serde_json's `arbitrary_precision` feature smuggles numbers through an object
/// with this key; a token must not be able to forge one.
const NUMBER_MARKER: &[u8] = b"$serde_json::private";

fn finite(value: &Value) -> bool {
    match value {
        Value::Number(n) => !n.is_f64() || n.as_f64().is_some_and(f64::is_finite),
        Value::Array(items) => items.iter().all(finite),
        Value::Object(map) => match map.get("$serde_json::private::Number") {
            // A number the parser kept as text: only a real number carries this key.
            Some(Value::String(text)) => text.parse::<f64>().is_ok_and(f64::is_finite),
            _ => map.values().all(finite),
        },
        _ => true,
    }
}

fn object(bytes: &[u8]) -> Result<Map<String, Value>, Fail> {
    if bytes
        .windows(NUMBER_MARKER.len())
        .any(|w| w == NUMBER_MARKER)
    {
        return Err(Fail::Malformed);
    }
    match serde_json::from_slice::<Strict>(bytes) {
        Ok(Strict(Value::Object(map))) if map.values().all(finite) => Ok(map),
        _ => Err(Fail::Malformed),
    }
}

fn mac<M: Mac + KeyInit>(key: &[u8], message: &[u8]) -> Result<Vec<u8>, Fail> {
    let mut mac = <M as KeyInit>::new_from_slice(key).map_err(|_| Fail::Key)?;
    mac.update(message);
    Ok(mac.finalize().into_bytes().to_vec())
}

fn sign_bytes(key: &Key, message: &[u8]) -> Result<Vec<u8>, Fail> {
    match key.alg {
        Alg::Hs256 => mac::<Hmac<Sha256>>(&key.bytes, message),
        Alg::Hs384 => mac::<Hmac<Sha384>>(&key.bytes, message),
        Alg::Hs512 => mac::<Hmac<Sha512>>(&key.bytes, message),
        Alg::EdDsa => {
            if key.public {
                return Err(Fail::Key);
            }
            let seed: [u8; 32] = key.bytes.as_slice().try_into().map_err(|_| Fail::Key)?;
            Ok(SigningKey::from_bytes(&seed)
                .sign(message)
                .to_bytes()
                .to_vec())
        }
    }
}

fn verify_signature(key: &Key, message: &[u8], signature: &[u8]) -> Result<(), Fail> {
    match key.alg {
        Alg::EdDsa => {
            let public = if key.public {
                let raw: [u8; 32] = key.bytes.as_slice().try_into().map_err(|_| Fail::Key)?;
                VerifyingKey::from_bytes(&raw).map_err(|_| Fail::Key)?
            } else {
                let seed: [u8; 32] = key.bytes.as_slice().try_into().map_err(|_| Fail::Key)?;
                SigningKey::from_bytes(&seed).verifying_key()
            };
            let raw: [u8; 64] = signature.try_into().map_err(|_| Fail::Signature)?;
            public
                .verify_strict(message, &Signature::from_bytes(&raw))
                .map_err(|_| Fail::Signature)
        }
        _ => {
            // Recompute and compare in constant time.
            let expected = sign_bytes(key, message)?;
            if crate::crypto::equal(&expected, signature) {
                Ok(())
            } else {
                Err(Fail::Signature)
            }
        }
    }
}

/// Build a compact JWS for `claims` (a JSON object, already serialised).
pub fn encode(claims: &[u8], key: &Key) -> Result<String, Fail> {
    object(claims)?; // the payload must be a JSON object without duplicates
    let mut header = Map::new();
    header.insert("alg".into(), Value::String(key.alg.name().into()));
    header.insert("typ".into(), Value::String("JWT".into()));
    if let Some(kid) = &key.kid {
        header.insert("kid".into(), Value::String(kid.clone()));
    }
    let head = serde_json::to_vec(&Value::Object(header)).map_err(|_| Fail::Malformed)?;
    let signing = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(head),
        URL_SAFE_NO_PAD.encode(claims)
    );
    let signature = sign_bytes(key, signing.as_bytes())?;
    Ok(format!("{signing}.{}", URL_SAFE_NO_PAD.encode(signature)))
}

pub struct Rules<'a> {
    pub now: i64,
    pub leeway: i64,
    pub issuer: Option<&'a str>,
    pub audience: Option<&'a str>,
    pub require_exp: bool,
}

fn numeric(claims: &Map<String, Value>, name: &str) -> Result<Option<i64>, Fail> {
    match claims.get(name) {
        None => Ok(None),
        Some(Value::Number(n)) => {
            if let Some(int) = n.as_i64() {
                return Ok(Some(int));
            }
            match n.as_f64() {
                Some(f) if f.is_finite() && f.abs() < 9.0e15 => Ok(Some(f.floor() as i64)),
                _ => Err(Fail::Claims),
            }
        }
        Some(_) => Err(Fail::Claims),
    }
}

fn check_claims(claims: &Map<String, Value>, rules: &Rules<'_>) -> Result<(), Fail> {
    check_expiration(claims, rules)?;
    check_temporal_claims(claims, rules)?;
    check_issuer(claims, rules)?;
    check_audience(claims, rules)
}

fn check_expiration(claims: &Map<String, Value>, rules: &Rules<'_>) -> Result<(), Fail> {
    let leeway = rules.leeway.clamp(0, MAX_LEEWAY);
    match numeric(claims, "exp")? {
        // RFC 7519 §4.1.4: on or after `exp` the token must not be accepted.
        Some(exp) if rules.now >= exp.saturating_add(leeway) => return Err(Fail::Expired),
        None if rules.require_exp => return Err(Fail::Claims),
        _ => {}
    }
    Ok(())
}

fn check_temporal_claims(claims: &Map<String, Value>, rules: &Rules<'_>) -> Result<(), Fail> {
    let leeway = rules.leeway.clamp(0, MAX_LEEWAY);
    if let Some(nbf) = numeric(claims, "nbf")? {
        if rules.now.saturating_add(leeway) < nbf {
            return Err(Fail::NotYetValid);
        }
    }
    if let Some(iat) = numeric(claims, "iat")? {
        if iat > rules.now.saturating_add(leeway) {
            return Err(Fail::NotYetValid);
        }
    }
    Ok(())
}

fn check_issuer(claims: &Map<String, Value>, rules: &Rules<'_>) -> Result<(), Fail> {
    if rules
        .issuer
        .is_some_and(|expected| claims.get("iss").and_then(Value::as_str) != Some(expected))
    {
        return Err(Fail::Issuer);
    }
    Ok(())
}

fn check_audience(claims: &Map<String, Value>, rules: &Rules<'_>) -> Result<(), Fail> {
    if rules.audience.is_none() && claims.contains_key("aud") {
        // RFC 7519 4.1.3: a token addressed to someone must not be accepted by a
        // verifier that does not say who it is.
        return Err(Fail::Audience);
    }
    if let Some(expected) = rules.audience {
        let ok = match claims.get("aud") {
            Some(Value::String(one)) => one == expected,
            Some(Value::Array(many)) => {
                many.iter().all(Value::is_string)
                    && many.iter().any(|v| v.as_str() == Some(expected))
            }
            _ => false,
        };
        if !ok {
            return Err(Fail::Audience);
        }
    }
    Ok(())
}

fn pick<'k>(keys: &'k [Key], kid: Option<&str>) -> Result<&'k Key, Fail> {
    match kid {
        Some(wanted) => keys
            .iter()
            .find(|k| k.kid.as_deref() == Some(wanted))
            .ok_or(Fail::Key),
        None if keys.len() == 1 => Ok(&keys[0]),
        None => Err(Fail::Key), // several keys: the token must say which
    }
}

fn segment(text: &str) -> Result<Vec<u8>, Fail> {
    URL_SAFE_NO_PAD.decode(text).map_err(|_| Fail::Malformed)
}

/// Verify `token` against `keys` and `rules`; returns the payload bytes and
/// parsed claims object in a single JSON parse.
fn decode_verified(
    token: &str,
    keys: &[Key],
    rules: &Rules<'_>,
) -> Result<(Vec<u8>, Map<String, Value>), Fail> {
    if token.len() > MAX_TOKEN || keys.is_empty() {
        return Err(Fail::Malformed);
    }
    let mut parts = token.split('.');
    let (Some(head_text), Some(body_text), Some(sig_text), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(Fail::Malformed);
    };
    let header = object(&segment(head_text)?)?;
    if header.contains_key("crit") || header.contains_key("zip") {
        return Err(Fail::Algorithm);
    }
    let kid = match header.get("kid") {
        None => None,
        Some(Value::String(text)) => Some(text.as_str()),
        Some(_) => return Err(Fail::Malformed),
    };
    let key = pick(keys, kid)?;
    match header.get("alg").and_then(Value::as_str) {
        Some(name) if name == key.alg.name() => {}
        _ => return Err(Fail::Algorithm),
    }
    let message = &token.as_bytes()[..head_text.len() + 1 + body_text.len()];
    verify_signature(key, message, &segment(sig_text)?)?;
    let payload = segment(body_text)?;
    let claims = object(&payload)?;
    check_claims(&claims, rules)?;
    Ok((payload, claims))
}

/// Verify `token` against `keys` and `rules`; returns the payload bytes (the
/// claims JSON) on success.
pub fn decode(token: &str, keys: &[Key], rules: &Rules<'_>) -> Result<Vec<u8>, Fail> {
    decode_verified(token, keys, rules).map(|(payload, _)| payload)
}

/// Verify `token` against `keys` and `rules`; returns the parsed claims map on
/// success without requiring a second JSON parse.
#[cfg(test)]
pub fn decode_claims(
    token: &str,
    keys: &[Key],
    rules: &Rules<'_>,
) -> Result<Map<String, Value>, Fail> {
    decode_verified(token, keys, rules).map(|(_, claims)| claims)
}

fn key_from(kid: Option<String>, algorithm: &str, bytes: Vec<u8>, public: bool) -> PyResult<Key> {
    let alg = Alg::parse(algorithm)
        .ok_or_else(|| PyValueError::new_err(format!("unsupported algorithm {algorithm:?}")))?;
    if let Some(min) = alg.min_secret() {
        if public || bytes.len() < min {
            return Err(PyValueError::new_err(format!(
                "{algorithm} needs a secret of at least {min} bytes"
            )));
        }
    }
    Ok(Key {
        kid,
        alg,
        bytes,
        public,
    })
}

/// `(kid, algorithm, key bytes, is_public)` as handed over by `hinoki.crypto`.
type KeySpec = (Option<String>, String, Vec<u8>, bool);

#[pyfunction]
pub fn jwt_encode(claims: &[u8], key: KeySpec) -> PyResult<String> {
    let key = key_from(key.0, &key.1, key.2, key.3)?;
    encode(claims, &key).map_err(|f| PyValueError::new_err(f.reason()))
}

/// Verify a token; returns the claims JSON bytes. Raises `ValueError` whose
/// message is the internal reason (never show it to a client).
/// Rules are `(now, leeway, issuer, audience, require_exp)`.
type RuleSpec = (i64, i64, Option<String>, Option<String>, bool);

fn decode_segment_with<R>(text: &str, f: impl FnOnce(&[u8]) -> Result<R, Fail>) -> Result<R, Fail> {
    if text.len() <= 680 {
        let mut buf = [0u8; 512];
        let len = URL_SAFE_NO_PAD
            .decode_slice(text, &mut buf)
            .map_err(|_| Fail::Malformed)?;
        return f(&buf[..len]);
    }
    let decoded = URL_SAFE_NO_PAD.decode(text).map_err(|_| Fail::Malformed)?;
    f(&decoded)
}

#[derive(Clone)]
enum CompiledVerifier {
    Hs256(Hmac<Sha256>),
    Hs384(Hmac<Sha384>),
    Hs512(Hmac<Sha512>),
    EdDsa(VerifyingKey),
}

struct CompiledKey {
    kid: Option<String>,
    alg: Alg,
    verifier: CompiledVerifier,
}

impl CompiledKey {
    fn from_key(key: &Key) -> Result<Self, Fail> {
        let verifier = match key.alg {
            Alg::Hs256 => CompiledVerifier::Hs256(
                Hmac::<Sha256>::new_from_slice(&key.bytes).map_err(|_| Fail::Key)?,
            ),
            Alg::Hs384 => CompiledVerifier::Hs384(
                Hmac::<Sha384>::new_from_slice(&key.bytes).map_err(|_| Fail::Key)?,
            ),
            Alg::Hs512 => CompiledVerifier::Hs512(
                Hmac::<Sha512>::new_from_slice(&key.bytes).map_err(|_| Fail::Key)?,
            ),
            Alg::EdDsa => {
                let public = if key.public {
                    let raw: [u8; 32] = key.bytes.as_slice().try_into().map_err(|_| Fail::Key)?;
                    VerifyingKey::from_bytes(&raw).map_err(|_| Fail::Key)?
                } else {
                    let seed: [u8; 32] = key.bytes.as_slice().try_into().map_err(|_| Fail::Key)?;
                    SigningKey::from_bytes(&seed).verifying_key()
                };
                CompiledVerifier::EdDsa(public)
            }
        };
        Ok(Self {
            kid: key.kid.clone(),
            alg: key.alg,
            verifier,
        })
    }

    fn verify(&self, message: &[u8], signature: &[u8]) -> Result<(), Fail> {
        match &self.verifier {
            CompiledVerifier::Hs256(base) => {
                let mut mac = base.clone();
                mac.update(message);
                mac.verify_slice(signature).map_err(|_| Fail::Signature)
            }
            CompiledVerifier::Hs384(base) => {
                let mut mac = base.clone();
                mac.update(message);
                mac.verify_slice(signature).map_err(|_| Fail::Signature)
            }
            CompiledVerifier::Hs512(base) => {
                let mut mac = base.clone();
                mac.update(message);
                mac.verify_slice(signature).map_err(|_| Fail::Signature)
            }
            CompiledVerifier::EdDsa(public) => {
                let raw: [u8; 64] = signature.try_into().map_err(|_| Fail::Signature)?;
                public
                    .verify_strict(message, &Signature::from_bytes(&raw))
                    .map_err(|_| Fail::Signature)
            }
        }
    }
}

fn pick_compiled<'k>(keys: &'k [CompiledKey], kid: Option<&str>) -> Result<&'k CompiledKey, Fail> {
    match kid {
        Some(wanted) => keys
            .iter()
            .find(|k| k.kid.as_deref() == Some(wanted))
            .ok_or(Fail::Key),
        None if keys.len() == 1 => Ok(&keys[0]),
        None => Err(Fail::Key),
    }
}

fn decode_claims_compiled(
    token: &str,
    keys: &[CompiledKey],
    rules: &Rules<'_>,
) -> Result<Map<String, Value>, Fail> {
    if token.len() > MAX_TOKEN || keys.is_empty() {
        return Err(Fail::Malformed);
    }
    let mut parts = token.split('.');
    let (Some(head_text), Some(body_text), Some(sig_text), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(Fail::Malformed);
    };
    let header = decode_segment_with(head_text, object)?;
    if header.contains_key("crit") || header.contains_key("zip") {
        return Err(Fail::Algorithm);
    }
    let kid = match header.get("kid") {
        None => None,
        Some(Value::String(text)) => Some(text.as_str()),
        Some(_) => return Err(Fail::Malformed),
    };
    let key = pick_compiled(keys, kid)?;
    match header.get("alg").and_then(Value::as_str) {
        Some(name) if name == key.alg.name() => {}
        _ => return Err(Fail::Algorithm),
    }
    let message = &token.as_bytes()[..head_text.len() + 1 + body_text.len()];
    decode_segment_with(sig_text, |sig| key.verify(message, sig))?;
    let claims = decode_segment_with(body_text, object)?;
    check_claims(&claims, rules)?;
    Ok(claims)
}

/// Parsed immutable verifier configuration. Requests can retain a cloned
/// Python handle while another request replaces the provider's cached handle.
struct Verifier {
    keys: Vec<Key>,
    compiled: Vec<CompiledKey>,
}

impl Verifier {
    fn from_specs(keys: Vec<KeySpec>) -> PyResult<Self> {
        let mut built = Vec::with_capacity(keys.len());
        let mut compiled = Vec::with_capacity(keys.len());
        for (kid, algorithm, bytes, public) in keys {
            let key = key_from(kid, &algorithm, bytes, public)?;
            compiled
                .push(CompiledKey::from_key(&key).map_err(|f| PyValueError::new_err(f.reason()))?);
            built.push(key);
        }
        Ok(Self {
            keys: built,
            compiled,
        })
    }

    fn verify(&self, token: &str, rules: RuleSpec) -> Result<Vec<u8>, Fail> {
        let (now, leeway, issuer, audience, require_exp) = rules;
        let rules = Rules {
            now,
            leeway,
            issuer: issuer.as_deref(),
            audience: audience.as_deref(),
            require_exp,
        };
        decode(token, &self.keys, &rules)
    }

    fn verify_claims(&self, token: &str, rules: RuleSpec) -> Result<Map<String, Value>, Fail> {
        let (now, leeway, issuer, audience, require_exp) = rules;
        let rules = Rules {
            now,
            leeway,
            issuer: issuer.as_deref(),
            audience: audience.as_deref(),
            require_exp,
        };
        decode_claims_compiled(token, &self.compiled, &rules)
    }
}

fn number_text_to_py<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    if text.contains(['.', 'e', 'E']) {
        let float = text
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .ok_or_else(|| PyValueError::new_err(Fail::Malformed.reason()))?;
        return Ok(PyFloat::new(py, float).into_any());
    }
    py.get_type::<PyInt>().call1((text,))
}

fn number_to_py<'py>(py: Python<'py>, number: &serde_json::Number) -> PyResult<Bound<'py, PyAny>> {
    if let Some(int) = number.as_i64() {
        return Ok(PyInt::new(py, int).into_any());
    }
    if let Some(uint) = number.as_u64() {
        return Ok(PyInt::new(py, uint).into_any());
    }
    number_text_to_py(py, number.as_str())
}

fn value_to_py<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Value::Null => Ok(py.None().into_bound(py)),
        Value::Bool(flag) => Ok(PyBool::new(py, *flag).to_owned().into_any()),
        Value::Number(number) => number_to_py(py, number),
        Value::String(text) => Ok(PyString::new(py, text).into_any()),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(value_to_py(py, item)?)?;
            }
            Ok(list.into_any())
        }
        Value::Object(map) => {
            if map.len() == 1 {
                if let Some(Value::String(text)) = map.get("$serde_json::private::Number") {
                    return number_text_to_py(py, text);
                }
            }
            Ok(map_to_py_dict(py, map)?.into_any())
        }
    }
}

pub(crate) fn map_to_py_dict<'py>(
    py: Python<'py>,
    map: &Map<String, Value>,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    for (key, value) in map {
        out.set_item(key, value_to_py(py, value)?)?;
    }
    Ok(out)
}

/// Native admission for a claims-only provider (`hinoki.crypto.ClaimsJWTAuth`).
///
/// It only ever says "admitted": every other outcome (no header, a header it does
/// not read exactly as Python does, a token or claims Python might refuse) is
/// left to the Python pipeline, which authenticates and answers `401`/`403`.
pub(crate) struct ClaimsAuth {
    compiled_keys: Vec<CompiledKey>,
    issuer: Option<String>,
    audience: Option<String>,
    leeway: i64,
    require_exp: bool,
    scope_claim: String,
    required: Vec<String>,
    extra_claims: Vec<String>,
    cookie: Option<String>,
}

fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
}

pub(crate) type ClaimList = Vec<(String, Option<String>)>;

enum CookieExtract<'a> {
    Missing,
    Token(&'a str),
    Defer,
}

fn trim_ascii(mut bytes: &[u8]) -> &[u8] {
    while let Some((&b, rest)) = bytes.split_first() {
        if !b.is_ascii_whitespace() {
            break;
        }
        bytes = rest;
    }
    while let Some((&b, rest)) = bytes.split_last() {
        if !b.is_ascii_whitespace() {
            break;
        }
        bytes = rest;
    }
    bytes
}

/// Extract a single plain-token cookie value matching `name`. Duplicate
/// matching cookies, empty values, or non-token bytes `Defer` to Python so both
/// sides never disagree on cookie parsing edge cases.
fn plain_cookie<'a>(header: &'a [u8], name: &[u8]) -> CookieExtract<'a> {
    let mut found: Option<&'a str> = None;
    for part in header.split(|&b| b == b';') {
        let Some(eq) = part.iter().position(|&b| b == b'=') else {
            continue;
        };
        if trim_ascii(&part[..eq]) != name {
            continue;
        }
        if found.is_some() {
            return CookieExtract::Defer;
        }
        let val = trim_ascii(&part[eq + 1..]);
        if val.is_empty() || !val.iter().copied().all(is_token_byte) {
            return CookieExtract::Defer;
        }
        let Ok(text) = std::str::from_utf8(val) else {
            return CookieExtract::Defer;
        };
        found = Some(text);
    }
    found.map_or(CookieExtract::Missing, CookieExtract::Token)
}

fn single_header<'a>(
    headers: &'a [(Vec<u8>, Vec<u8>)],
    wanted: &[u8],
) -> Result<Option<&'a [u8]>, ()> {
    let mut iter = headers
        .iter()
        .filter(|(name, _)| name.eq_ignore_ascii_case(wanted));
    let first = iter.next().map(|(_, value)| value.as_slice());
    if iter.next().is_some() {
        return Err(());
    }
    Ok(first)
}

impl ClaimsAuth {
    /// The `sub` and any requested non-empty string claims of a valid
    /// `Authorization: Bearer <token>` with every required permission.
    #[cfg(test)]
    pub(crate) fn caller(&self, authorization: &[u8], now: i64) -> Option<(String, ClaimList)> {
        match self.admit(Some(authorization), now) {
            Admission::Admit {
                subject, claims, ..
            } => Some((subject, claims)),
            _ => None,
        }
    }

    /// The `sub` and requested claims from either the configured cookie or
    /// `Authorization: Bearer <token>`.
    pub(crate) fn caller_from_header(
        &self,
        header_value: &[u8],
        now: i64,
    ) -> Option<(String, ClaimList)> {
        let admission = if let Some(cookie_name) = self.cookie.as_deref() {
            self.admit_cookie(Some(header_value), cookie_name.as_bytes(), now)
        } else {
            self.admit(Some(header_value), now)
        };
        match admission {
            Admission::Admit {
                subject, claims, ..
            } => Some((subject, claims)),
            _ => None,
        }
    }

    pub(crate) fn credential_header_name(&self) -> &'static [u8] {
        if self.cookie.is_some() {
            b"cookie"
        } else {
            b"authorization"
        }
    }

    /// Admit from raw request headers using either the configured cookie name
    /// or `Authorization: Bearer <token>`.
    pub(crate) fn admit_headers(&self, headers: &[(Vec<u8>, Vec<u8>)], now: i64) -> Admission {
        if let Some(cookie_name) = self.cookie.as_deref() {
            let Ok(raw) = single_header(headers, b"cookie") else {
                return Admission::Defer;
            };
            return self.admit_cookie(raw, cookie_name.as_bytes(), now);
        }
        let Ok(raw) = single_header(headers, b"authorization") else {
            return Admission::Defer;
        };
        self.admit(raw, now)
    }

    fn verify_token(&self, token: &str, now: i64) -> Admission {
        let rules = Rules {
            now,
            leeway: self.leeway,
            issuer: self.issuer.as_deref(),
            audience: self.audience.as_deref(),
            require_exp: self.require_exp,
        };
        match decode_claims_compiled(token, &self.compiled_keys, &rules) {
            Ok(claims) => self.grants(claims),
            Err(_) => Admission::Invalid,
        }
    }

    /// What Python's `authorize` decides for one `Cookie` header when `cookie`
    /// is configured.
    pub(crate) fn admit_cookie(
        &self,
        cookie_header: Option<&[u8]>,
        cookie_name: &[u8],
        now: i64,
    ) -> Admission {
        let Some(header) = cookie_header else {
            return Admission::Missing;
        };
        match plain_cookie(header, cookie_name) {
            CookieExtract::Missing => Admission::Missing,
            CookieExtract::Defer => Admission::Defer,
            CookieExtract::Token(token) => self.verify_token(token, now),
        }
    }

    /// What Python's `authorize` decides for one `Authorization` header (absent
    /// or appearing once). Only outcomes both sides read identically are
    /// decided: a header that is not a plain bearer token is `Defer`red.
    pub(crate) fn admit(&self, authorization: Option<&[u8]>, now: i64) -> Admission {
        let Some(authorization) = authorization else {
            return Admission::Missing;
        };
        let Some(token) = plain_bearer(authorization) else {
            return Admission::Defer;
        };
        self.verify_token(token, now)
    }

    fn grants(&self, claims: Map<String, Value>) -> Admission {
        let subject = match claims.get("sub") {
            Some(Value::String(subject)) if !subject.is_empty() => subject.clone(),
            _ => return Admission::Invalid,
        };
        let scopes: Vec<String> = match claims.get(&self.scope_claim) {
            None => Vec::new(),
            Some(Value::Array(items)) => items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect(),
            Some(_) => return Admission::Invalid,
        };
        if !self.required.iter().all(|need| scopes.contains(need)) {
            return Admission::Forbidden;
        }
        let extracted = self
            .extra_claims
            .iter()
            .map(|name| {
                let value = claims
                    .get(name)
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(str::to_owned);
                (name.clone(), value)
            })
            .collect();
        Admission::Admit {
            subject,
            scopes,
            claims: extracted,
            raw_claims: claims,
        }
    }
}

/// The token of `Bearer <token>` when it is plain enough that Python's
/// whitespace stripping cannot make the two readings differ.
fn plain_bearer(authorization: &[u8]) -> Option<&str> {
    let space = authorization.iter().position(|byte| *byte == b' ')?;
    let (scheme, token) = (&authorization[..space], &authorization[space + 1..]);
    if !scheme.eq_ignore_ascii_case(b"bearer")
        || token.is_empty()
        || !token.iter().copied().all(is_token_byte)
    {
        return None;
    }
    std::str::from_utf8(token).ok()
}

/// The verdict of [`ClaimsAuth::admit`].
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Admission {
    /// Authenticated, with every permission the route requires; `scopes` are all
    /// the string scopes of the token, `claims` are any extra non-empty string
    /// claims requested by a CRUD plan, and `raw_claims` is the verified claim map.
    Admit {
        subject: String,
        scopes: Vec<String>,
        claims: Vec<(String, Option<String>)>,
        raw_claims: Map<String, Value>,
    },
    /// No credentials: Python answers `401`.
    Missing,
    /// Credentials Python would refuse: `401`.
    Invalid,
    /// Authenticated without the required permissions: `403`.
    Forbidden,
    /// Not read the same way on both sides; Python decides.
    Defer,
}

/// Private immutable claims-only authentication settings of one CRUD route.
#[pyclass(name = "_ClaimsAuth", module = "hinoki.core", frozen)]
pub(crate) struct PyClaimsAuth {
    pub(crate) inner: std::sync::Arc<ClaimsAuth>,
}

#[pymethods]
impl PyClaimsAuth {
    #[new]
    #[pyo3(signature = (
        keys, issuer, audience, leeway, require_exp, scope_claim, required,
        extra_claims=Vec::new(),
        cookie=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        keys: Vec<KeySpec>,
        issuer: Option<String>,
        audience: Option<String>,
        leeway: i64,
        require_exp: bool,
        scope_claim: String,
        required: Vec<String>,
        extra_claims: Vec<String>,
        cookie: Option<String>,
    ) -> PyResult<Self> {
        if keys.is_empty() || !(0..=MAX_LEEWAY).contains(&leeway) {
            return Err(PyValueError::new_err("invalid claims authentication"));
        }
        let compiled_keys = keys
            .into_iter()
            .map(|(kid, algorithm, bytes, public)| {
                let key = key_from(kid, &algorithm, bytes, public)?;
                CompiledKey::from_key(&key)
                    .map_err(|_| PyValueError::new_err("invalid claims authentication"))
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            inner: std::sync::Arc::new(ClaimsAuth {
                compiled_keys,
                issuer,
                audience,
                leeway,
                require_exp,
                scope_claim,
                required,
                extra_claims,
                cookie,
            }),
        })
    }
}

/// Private immutable native verifier; Python owns a short-lived reference.
#[pyclass(name = "_JWTVerifier", module = "hinoki.core", frozen)]
pub(super) struct PyJWTVerifier {
    inner: Verifier,
}

#[pymethods]
impl PyJWTVerifier {
    #[new]
    fn new(keys: Vec<KeySpec>) -> PyResult<Self> {
        Ok(Self {
            inner: Verifier::from_specs(keys)?,
        })
    }

    fn verify(
        &self,
        py: Python<'_>,
        token: &str,
        rules: RuleSpec,
    ) -> PyResult<Py<pyo3::types::PyBytes>> {
        let payload = self
            .inner
            .verify(token, rules)
            .map_err(|failure| PyValueError::new_err(failure.reason()))?;
        Ok(pyo3::types::PyBytes::new(py, &payload).unbind())
    }

    fn verify_claims<'py>(
        &self,
        py: Python<'py>,
        token: &str,
        rules: RuleSpec,
    ) -> PyResult<Bound<'py, PyDict>> {
        let claims = self
            .inner
            .verify_claims(token, rules)
            .map_err(|failure| PyValueError::new_err(failure.reason()))?;
        map_to_py_dict(py, &claims)
    }
}

#[pyfunction]
pub fn jwt_decode(
    py: Python<'_>,
    token: &str,
    keys: Vec<KeySpec>,
    rules: RuleSpec,
) -> PyResult<Py<pyo3::types::PyBytes>> {
    let verifier = Verifier::from_specs(keys)?;
    let payload = verifier
        .verify(token, rules)
        .map_err(|failure| PyValueError::new_err(failure.reason()))?;
    Ok(pyo3::types::PyBytes::new(py, &payload).unbind())
}

#[cfg(test)]
mod tests {
    trait Admits {
        fn admits(&self, authorization: &[u8], now: i64) -> bool;
    }

    impl Admits for ClaimsAuth {
        fn admits(&self, authorization: &[u8], now: i64) -> bool {
            self.caller(authorization, now).is_some()
        }
    }

    use super::*;

    fn hs(kid: Option<&str>) -> Key {
        Key {
            kid: kid.map(str::to_owned),
            alg: Alg::Hs256,
            bytes: vec![7; 32],
            public: false,
        }
    }

    fn rules(now: i64) -> Rules<'static> {
        Rules {
            now,
            leeway: 0,
            issuer: None,
            audience: None,
            require_exp: true,
        }
    }

    #[test]
    fn a_token_verifies_with_its_key_only() {
        let token = encode(br#"{"sub":"a","exp":2000}"#, &hs(None)).unwrap();
        assert!(decode(&token, &[hs(None)], &rules(1000)).is_ok());
        let other = Key {
            bytes: vec![8; 32],
            ..hs(None)
        };
        assert_eq!(decode(&token, &[other], &rules(1000)), Err(Fail::Signature));
    }

    #[test]
    fn the_algorithm_is_compared_never_chosen_by_the_token() {
        let token = encode(br#"{"exp":2000}"#, &hs(None)).unwrap();
        let mut forged: Vec<&str> = token.split('.').collect();
        let none = URL_SAFE_NO_PAD.encode(br#"{"alg":"none"}"#);
        forged[0] = &none;
        assert_eq!(
            decode(&forged.join("."), &[hs(None)], &rules(1)),
            Err(Fail::Algorithm)
        );
        let other = Key {
            alg: Alg::Hs512,
            bytes: vec![7; 64],
            ..hs(None)
        };
        assert_eq!(decode(&token, &[other], &rules(1)), Err(Fail::Algorithm));
    }

    #[test]
    fn claims_are_checked_with_the_given_clock() {
        let token = encode(br#"{"exp":1000,"nbf":500}"#, &hs(None)).unwrap();
        assert!(decode(&token, &[hs(None)], &rules(700)).is_ok());
        assert_eq!(
            decode(&token, &[hs(None)], &rules(1001)),
            Err(Fail::Expired)
        );
        assert_eq!(
            decode(&token, &[hs(None)], &rules(499)),
            Err(Fail::NotYetValid)
        );
        let slack = Rules {
            leeway: 5,
            ..rules(1004)
        };
        assert!(decode(&token, &[hs(None)], &slack).is_ok());
        let no_exp = encode(br#"{"sub":"a"}"#, &hs(None)).unwrap();
        assert_eq!(decode(&no_exp, &[hs(None)], &rules(1)), Err(Fail::Claims));
    }

    #[test]
    fn issuer_and_audience_must_match() {
        let token = encode(br#"{"exp":9,"iss":"me","aud":["a","b"]}"#, &hs(None)).unwrap();
        let ok = Rules {
            issuer: Some("me"),
            audience: Some("b"),
            ..rules(1)
        };
        assert!(decode(&token, &[hs(None)], &ok).is_ok());
        assert_eq!(
            decode(
                &token,
                &[hs(None)],
                &Rules {
                    issuer: Some("x"),
                    ..rules(1)
                }
            ),
            Err(Fail::Issuer)
        );
        assert_eq!(
            decode(
                &token,
                &[hs(None)],
                &Rules {
                    audience: Some("c"),
                    ..rules(1)
                }
            ),
            Err(Fail::Audience)
        );
        let mixed = encode(br#"{"exp":9,"aud":["a",1]}"#, &hs(None)).unwrap();
        assert_eq!(
            decode(
                &mixed,
                &[hs(None)],
                &Rules {
                    audience: Some("a"),
                    ..rules(1)
                }
            ),
            Err(Fail::Audience)
        );
    }

    #[test]
    fn a_token_addressed_to_someone_needs_a_verifier_that_names_itself() {
        let token = encode(br#"{"exp":9,"aud":"other-service"}"#, &hs(None)).unwrap();
        assert_eq!(decode(&token, &[hs(None)], &rules(1)), Err(Fail::Audience));
        let plain = encode(br#"{"exp":9}"#, &hs(None)).unwrap();
        assert!(decode(&plain, &[hs(None)], &rules(1)).is_ok());
    }

    #[test]
    fn nested_duplicates_and_forged_number_markers_are_refused() {
        let nested = br#"{"exp":9,"r":{"a":1,"a":2}}"#;
        assert_eq!(encode(nested, &hs(None)), Err(Fail::Malformed));
        let forged = br#"{"exp":{"$serde_json::private::Number":"5000"}}"#;
        assert_eq!(encode(forged, &hs(None)), Err(Fail::Malformed));
    }

    #[test]
    fn several_keys_need_a_kid_and_unknown_kids_fail() {
        let keys = [
            hs(Some("a")),
            Key {
                bytes: vec![9; 32],
                ..hs(Some("b"))
            },
        ];
        let token = encode(br#"{"exp":9}"#, &keys[1]).unwrap();
        assert!(decode(&token, &keys, &rules(1)).is_ok());
        let plain = encode(br#"{"exp":9}"#, &hs(None)).unwrap();
        assert_eq!(decode(&plain, &keys, &rules(1)), Err(Fail::Key));
        assert_eq!(decode(&token, &keys[..1], &rules(1)), Err(Fail::Key));
    }

    #[test]
    fn malformed_tokens_are_refused() {
        let good = encode(br#"{"exp":9}"#, &hs(None)).unwrap();
        for bad in [
            "",
            "a.b",
            "a.b.c.d",
            &format!("{good}="),
            &format!("{good}\n"),
            &"a".repeat(MAX_TOKEN + 1),
            "!!!.###.$$$",
        ] {
            assert!(decode(bad, &[hs(None)], &rules(1)).is_err(), "{bad:.20}");
        }
        let dup = URL_SAFE_NO_PAD.encode(br#"{"alg":"HS256","alg":"HS256"}"#);
        let body = URL_SAFE_NO_PAD.encode(br#"{"exp":9}"#);
        assert_eq!(
            decode(&format!("{dup}.{body}.AAAA"), &[hs(None)], &rules(1)),
            Err(Fail::Malformed)
        );
        let crit = URL_SAFE_NO_PAD.encode(br#"{"alg":"HS256","crit":["x"]}"#);
        assert_eq!(
            decode(&format!("{crit}.{body}.AAAA"), &[hs(None)], &rules(1)),
            Err(Fail::Algorithm)
        );
    }

    #[test]
    fn a_payload_number_out_of_float_range_is_refused() {
        for payload in [
            &br#"{"exp":9,"x":1e400}"#[..],
            &br#"{"exp":9,"x":[-1e999]}"#[..],
        ] {
            assert!(object(payload).is_err(), "{payload:?}");
        }
        assert!(object(br#"{"exp":9,"x":1.5e300}"#).is_ok());
    }

    #[test]
    fn ed25519_signs_and_a_public_key_verifies() {
        let seed = vec![3_u8; 32];
        let private = Key {
            kid: None,
            alg: Alg::EdDsa,
            bytes: seed.clone(),
            public: false,
        };
        let token = encode(br#"{"exp":9}"#, &private).unwrap();
        let public = SigningKey::from_bytes(&seed.as_slice().try_into().unwrap()).verifying_key();
        let verify_only = Key {
            kid: None,
            alg: Alg::EdDsa,
            bytes: public.to_bytes().to_vec(),
            public: true,
        };
        assert!(decode(&token, &[verify_only], &rules(1)).is_ok());
        assert_eq!(
            encode(
                br#"{"exp":9}"#,
                &Key {
                    public: true,
                    ..private
                }
            ),
            Err(Fail::Key)
        );
    }

    #[test]
    fn hmac_secrets_shorter_than_the_hash_are_refused() {
        for (algorithm, min) in [("HS256", 32), ("HS384", 48), ("HS512", 64)] {
            assert!(key_from(None, algorithm, vec![1; min], false).is_ok());
            assert!(key_from(None, algorithm, vec![1; min - 1], false).is_err());
            assert!(key_from(None, algorithm, Vec::new(), false).is_err());
            assert!(key_from(None, algorithm, vec![1; min], true).is_err());
            let short = vec![(None, algorithm.to_owned(), vec![1; min - 1], false)];
            assert!(Verifier::from_specs(short).is_err());
        }
        assert!(key_from(None, "EdDSA", vec![4; 32], false).is_ok());
    }

    #[test]
    fn immutable_verifier_matches_fresh_decode_for_every_algorithm() {
        let algorithms = [
            ("HS256", vec![1; 32]),
            ("HS384", vec![2; 48]),
            ("HS512", vec![3; 64]),
            ("EdDSA", vec![4; 32]),
        ];
        for (algorithm, bytes) in algorithms {
            let key = key_from(None, algorithm, bytes.clone(), false).unwrap();
            let token = encode(br#"{"sub":"rust","exp":2000}"#, &key).unwrap();
            let verifier =
                Verifier::from_specs(vec![(None, algorithm.to_owned(), bytes, false)]).unwrap();
            let policy = rules(1000);
            let expected = decode(&token, &verifier.keys, &policy);
            assert_eq!(
                verifier.verify(&token, (1000, 0, None, None, true)),
                expected
            );

            let mut invalid = token;
            invalid.pop();
            invalid.push('x');
            assert_eq!(
                verifier.verify(&invalid, (1000, 0, None, None, true)),
                decode(&invalid, &verifier.keys, &policy)
            );
        }
    }

    fn claims_auth(required: &[&str]) -> ClaimsAuth {
        ClaimsAuth {
            compiled_keys: vec![CompiledKey::from_key(&hs(None)).unwrap()],
            issuer: None,
            audience: None,
            leeway: 0,
            require_exp: true,
            scope_claim: "scopes".to_owned(),
            required: required.iter().map(|name| (*name).to_owned()).collect(),
            extra_claims: Vec::new(),
            cookie: None,
        }
    }

    fn bearer(claims: &str) -> Vec<u8> {
        format!("Bearer {}", encode(claims.as_bytes(), &hs(None)).unwrap()).into_bytes()
    }

    #[test]
    fn claims_auth_admits_a_valid_token_with_the_required_permissions() {
        let auth = claims_auth(&["posts:write"]);
        let header = bearer(r#"{"sub":"u1","exp":2000,"scopes":["posts:write","x",7]}"#);
        assert!(auth.admits(&header, 1000));
        assert!(claims_auth(&[]).admits(&bearer(r#"{"sub":"u1","exp":2000}"#), 1000));
    }

    #[test]
    fn claims_auth_leaves_every_doubtful_request_to_python() {
        let auth = claims_auth(&["posts:write"]);
        let good = r#"{"sub":"u1","exp":2000,"scopes":["posts:write"]}"#;
        assert!(!auth.admits(&bearer(good), 2000), "expired");
        assert!(!auth.admits(&bearer(r#"{"sub":"u1","exp":2000,"scopes":[]}"#), 1000));
        assert!(!auth.admits(&bearer(r#"{"exp":2000,"scopes":["posts:write"]}"#), 1000));
        assert!(!auth.admits(
            &bearer(r#"{"sub":"","exp":2000,"scopes":["posts:write"]}"#),
            1000
        ));
        assert!(!auth.admits(
            &bearer(r#"{"sub":"u","exp":2000,"scopes":"posts:write"}"#),
            1000
        ));
        let token = String::from_utf8(bearer(good)).unwrap();
        let token = token.strip_prefix("Bearer ").unwrap();
        assert!(auth.admits(format!("bEaReR {token}").as_bytes(), 1000));
        for header in [
            format!("Basic {token}"),
            format!("Bearer  {token}"),
            format!("Bearer {token} "),
            format!("Bearer {token}, Bearer {token}"),
            token.to_owned(),
            "Bearer ".to_owned(),
        ] {
            assert!(!auth.admits(header.as_bytes(), 1000), "{header}");
        }
        let mut forged = token.to_owned();
        forged.pop();
        forged.push('x');
        assert!(!auth.admits(format!("Bearer {forged}").as_bytes(), 1000));
    }

    #[test]
    fn claims_auth_names_the_answer_python_gives() {
        let auth = claims_auth(&["posts:write"]);
        let good = r#"{"sub":"u1","exp":2000,"scopes":["posts:write","x",7]}"#;
        assert_eq!(
            auth.admit(Some(&bearer(good)), 1000),
            Admission::Admit {
                subject: "u1".to_owned(),
                scopes: vec!["posts:write".to_owned(), "x".to_owned()],
                claims: Vec::new(),
                raw_claims: serde_json::from_str(good).unwrap(),
            }
        );
        assert_eq!(auth.admit(None, 1000), Admission::Missing);
        assert_eq!(auth.admit(Some(&bearer(good)), 2000), Admission::Invalid);
        assert_eq!(
            auth.admit(
                Some(&bearer(r#"{"sub":"u1","exp":2000,"scopes":[]}"#)),
                1000
            ),
            Admission::Forbidden
        );
        assert_eq!(
            auth.admit(
                Some(&bearer(r#"{"exp":2000,"scopes":["posts:write"]}"#)),
                1000
            ),
            Admission::Invalid
        );
        assert_eq!(auth.admit(Some(b"Basic abc"), 1000), Admission::Defer);
        assert_eq!(auth.admit(Some(b"Bearer "), 1000), Admission::Defer);
    }

    #[test]
    fn claims_auth_extracts_non_empty_string_extra_claims() {
        let mut auth = claims_auth(&[]);
        auth.extra_claims = vec!["tenant_id".to_owned(), "org".to_owned()];
        let header = bearer(r#"{"sub":"u1","exp":2000,"tenant_id":"acme","org":""}"#);
        assert_eq!(
            auth.caller(&header, 1000),
            Some((
                "u1".to_owned(),
                vec![
                    ("tenant_id".to_owned(), Some("acme".to_owned())),
                    ("org".to_owned(), None),
                ],
            ))
        );
        let non_str = bearer(r#"{"sub":"u1","exp":2000,"tenant_id":42}"#);
        assert_eq!(
            auth.caller(&non_str, 1000),
            Some((
                "u1".to_owned(),
                vec![("tenant_id".to_owned(), None), ("org".to_owned(), None)],
            ))
        );
    }

    #[test]
    fn decode_claims_matches_decode_and_checks_errors() {
        let key = hs(Some("k1"));
        let token = encode(
            br#"{"sub":"u1","exp":2000,"n":42,"ok":true,"tags":["a"]}"#,
            &key,
        )
        .unwrap();
        let claims = decode_claims(&token, &[key], &rules(1000)).unwrap();
        assert_eq!(claims.get("sub").and_then(Value::as_str), Some("u1"));
        assert_eq!(claims.get("n").and_then(Value::as_i64), Some(42));
        assert_eq!(claims.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(
            decode_claims(&token, &[hs(Some("k1"))], &rules(2000)),
            Err(Fail::Expired)
        );
        let mut wrong_key = hs(Some("k1"));
        wrong_key.bytes[0] ^= 1;
        assert_eq!(
            decode_claims(&token, &[wrong_key], &rules(1000)),
            Err(Fail::Signature)
        );
    }

    #[test]
    fn claims_auth_cookie_admits_valid_and_defers_ambiguous_headers() {
        let mut auth = claims_auth(&["posts:write"]);
        auth.cookie = Some("access_token".to_owned());
        let good = r#"{"sub":"u1","exp":2000,"scopes":["posts:write"]}"#;
        let token = encode(good.as_bytes(), &hs(None)).unwrap();

        let valid_headers = vec![(
            b"cookie".to_vec(),
            format!("theme=dark; access_token={token}; lang=en").into_bytes(),
        )];
        assert!(matches!(
            auth.admit_headers(&valid_headers, 1000),
            Admission::Admit { ref subject, .. } if subject == "u1"
        ));
        assert_eq!(
            auth.caller_from_header(&valid_headers[0].1, 1000),
            Some(("u1".to_owned(), Vec::new()))
        );
        assert_eq!(auth.admit_headers(&[], 1000), Admission::Missing);
        assert_eq!(
            auth.admit_headers(&[(b"cookie".to_vec(), b"other=1".to_vec())], 1000),
            Admission::Missing
        );
        assert_eq!(auth.admit_headers(&valid_headers, 2000), Admission::Invalid);
        let dup_cookie = vec![(
            b"cookie".to_vec(),
            format!("access_token={token}; access_token={token}").into_bytes(),
        )];
        assert_eq!(auth.admit_headers(&dup_cookie, 1000), Admission::Defer);
        let quoted_cookie = vec![(
            b"cookie".to_vec(),
            format!("access_token=\"{token}\"").into_bytes(),
        )];
        assert_eq!(auth.admit_headers(&quoted_cookie, 1000), Admission::Defer);
        let two_headers = vec![
            (b"cookie".to_vec(), b"a=1".to_vec()),
            (
                b"cookie".to_vec(),
                format!("access_token={token}").into_bytes(),
            ),
        ];
        assert_eq!(auth.admit_headers(&two_headers, 1000), Admission::Defer);
    }
}
