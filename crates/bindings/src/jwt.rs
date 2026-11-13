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

fn object(bytes: &[u8]) -> Result<Map<String, Value>, Fail> {
    if bytes
        .windows(NUMBER_MARKER.len())
        .any(|w| w == NUMBER_MARKER)
    {
        return Err(Fail::Malformed);
    }
    match serde_json::from_slice::<Strict>(bytes) {
        Ok(Strict(Value::Object(map))) => Ok(map),
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
    let leeway = rules.leeway.clamp(0, MAX_LEEWAY);
    match numeric(claims, "exp")? {
        Some(exp) if rules.now > exp.saturating_add(leeway) => return Err(Fail::Expired),
        None if rules.require_exp => return Err(Fail::Claims),
        _ => {}
    }
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
    if let Some(expected) = rules.issuer {
        if claims.get("iss").and_then(Value::as_str) != Some(expected) {
            return Err(Fail::Issuer);
        }
    }
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

/// Verify `token` against `keys` and `rules`; returns the payload bytes (the
/// claims JSON) on success.
pub fn decode(token: &str, keys: &[Key], rules: &Rules<'_>) -> Result<Vec<u8>, Fail> {
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
    check_claims(&object(&payload)?, rules)?;
    Ok(payload)
}

fn key_from(kid: Option<String>, algorithm: &str, bytes: Vec<u8>, public: bool) -> PyResult<Key> {
    let alg = Alg::parse(algorithm)
        .ok_or_else(|| PyValueError::new_err(format!("unsupported algorithm {algorithm:?}")))?;
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

#[pyfunction]
pub fn jwt_decode(
    py: Python<'_>,
    token: &str,
    keys: Vec<KeySpec>,
    rules: RuleSpec,
) -> PyResult<Py<pyo3::types::PyBytes>> {
    let mut built = Vec::with_capacity(keys.len());
    for (kid, algorithm, bytes, public) in keys {
        built.push(key_from(kid, &algorithm, bytes, public)?);
    }
    let (now, leeway, issuer, audience, require_exp) = rules;
    let rules = Rules {
        now,
        leeway,
        issuer: issuer.as_deref(),
        audience: audience.as_deref(),
        require_exp,
    };
    let payload = decode(token, &built, &rules).map_err(|f| PyValueError::new_err(f.reason()))?;
    Ok(pyo3::types::PyBytes::new(py, &payload).unbind())
}

#[cfg(test)]
mod tests {
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
}
