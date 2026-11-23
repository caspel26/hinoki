//! Bucket keys: normalised, length-prefixed and hashed, so nothing a client sends
//! reaches SQL, a log or a metric and a separator in a value cannot move the
//! boundary between two segments.

use std::net::{IpAddr, Ipv6Addr};

use percent_encoding::percent_decode_str;
use sha2::{Digest, Sha256};

/// Longest segment (before decoding) a key accepts, in bytes.
pub const MAX_SEGMENT: usize = 1024;
/// Percent-decoding passes: a value encoded more often stays partly encoded,
/// which is a different bucket and gains an attacker nothing over a random key.
const MAX_DECODES: usize = 4;
const PREFIX: &[u8] = b"hinoki-rl\x01";

const TAG_SCOPE: u8 = 1;
const TAG_CLIENT: u8 = 2;
const TAG_CALLER: u8 = 3;
const TAG_CUSTOM: u8 = 4;
const TAG_TENANT: u8 = 5;

/// The SHA-256 of a normalised key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Bucket(pub [u8; 32]);

impl Bucket {
    /// Lowercase hexadecimal, the form stored in the table.
    pub fn hex(&self) -> [u8; 64] {
        const DIGITS: &[u8; 16] = b"0123456789abcdef";
        let mut out = [0_u8; 64];
        for (index, byte) in self.0.iter().enumerate() {
            out[2 * index] = DIGITS[usize::from(byte >> 4)];
            out[2 * index + 1] = DIGITS[usize::from(byte & 0x0f)];
        }
        out
    }

    /// The hexadecimal form as text (ASCII by construction).
    pub fn hex_string(&self) -> String {
        self.hex().iter().map(|byte| char::from(*byte)).collect()
    }
}

/// Who a hit is counted against.
#[derive(Debug, Clone, Copy)]
pub enum Who<'a> {
    /// A client address; `None` when unknown (all unknown clients share a bucket).
    Client(Option<&'a str>),
    Caller(&'a str),
    Custom(&'a str),
}

#[derive(Debug, Clone, Copy)]
pub struct KeyParts<'a> {
    pub scope: &'a str,
    pub who: Who<'a>,
    pub tenant: Option<&'a str>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Keyed {
    pub bucket: Bucket,
    /// The client is unknown: the caller widens the quota for it.
    pub unknown: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyError {
    TooLong,
    InvalidTable,
}

fn decode_once(text: &str) -> Option<String> {
    if !text.contains('%') {
        return None;
    }
    let decoded = percent_decode_str(text).decode_utf8_lossy();
    (decoded != text).then(|| decoded.into_owned())
}

/// Trim, percent-decode to a fixed point (at most [`MAX_DECODES`] passes), trim,
/// lowercase ASCII. Invalid UTF-8 after decoding becomes U+FFFD, so two
/// different invalid values share a bucket: stricter, never more permissive.
fn normalise(segment: &str, out: &mut Vec<u8>) -> Result<(), KeyError> {
    if segment.len() > MAX_SEGMENT {
        return Err(KeyError::TooLong);
    }
    let trimmed = segment.trim();
    let mut owned: Option<String> = None;
    for _ in 0..MAX_DECODES {
        let current = owned.as_deref().unwrap_or(trimmed);
        match decode_once(current) {
            Some(next) => owned = Some(next),
            None => break,
        }
    }
    let text = owned.as_deref().unwrap_or(trimmed).trim();
    out.extend(text.bytes().map(|byte| byte.to_ascii_lowercase()));
    Ok(())
}

/// An address as its network: IPv4-mapped IPv6 as IPv4, IPv6 by its /64; anything
/// that is not an address as normalised text.
fn client(address: &str, out: &mut Vec<u8>) -> Result<(), KeyError> {
    if address.len() > MAX_SEGMENT {
        return Err(KeyError::TooLong);
    }
    match address.trim().parse::<IpAddr>() {
        Ok(IpAddr::V4(v4)) => {
            out.push(4);
            out.extend_from_slice(&v4.octets());
        }
        Ok(IpAddr::V6(v6)) => push_v6(v6, out),
        Err(_) => {
            out.push(0);
            normalise(address, out)?;
        }
    }
    Ok(())
}

fn push_v6(address: Ipv6Addr, out: &mut Vec<u8>) {
    if let Some(v4) = address.to_ipv4_mapped() {
        out.push(4);
        out.extend_from_slice(&v4.octets());
        return;
    }
    out.push(6);
    out.extend_from_slice(&address.octets()[..8]);
}

fn feed(hasher: &mut Sha256, tag: u8, bytes: &[u8]) -> Result<(), KeyError> {
    let length = u32::try_from(bytes.len()).map_err(|_| KeyError::TooLong)?;
    hasher.update([tag]);
    hasher.update(length.to_be_bytes());
    hasher.update(bytes);
    Ok(())
}

/// The bucket of a key. Each segment is hashed with its tag and its length, so a
/// caller named `1.2.3.4` is not the client `1.2.3.4`.
pub fn bucket(parts: &KeyParts<'_>) -> Result<Keyed, KeyError> {
    let mut hasher = Sha256::new();
    hasher.update(PREFIX);
    if parts.scope.len() > MAX_SEGMENT {
        return Err(KeyError::TooLong);
    }
    feed(&mut hasher, TAG_SCOPE, parts.scope.as_bytes())?;
    let mut buffer = Vec::with_capacity(64);
    let (tag, unknown) = match parts.who {
        Who::Client(Some(address)) => {
            client(address, &mut buffer)?;
            (TAG_CLIENT, false)
        }
        Who::Client(None) => (TAG_CLIENT, true),
        Who::Caller(id) => {
            normalise(id, &mut buffer)?;
            (TAG_CALLER, false)
        }
        Who::Custom(key) => {
            normalise(key, &mut buffer)?;
            (TAG_CUSTOM, false)
        }
    };
    feed(&mut hasher, tag, &buffer)?;
    if let Some(tenant) = parts.tenant {
        buffer.clear();
        normalise(tenant, &mut buffer)?;
        feed(&mut hasher, TAG_TENANT, &buffer)?;
    }
    Ok(Keyed {
        bucket: Bucket(hasher.finalize().into()),
        unknown,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn of(who: Who<'_>, tenant: Option<&str>) -> Bucket {
        bucket(&KeyParts {
            scope: "GET /x",
            who,
            tenant,
        })
        .unwrap()
        .bucket
    }

    #[test]
    fn case_whitespace_and_percent_encoding_do_not_make_new_buckets() {
        let plain = of(Who::Caller("alice"), None);
        for variant in [
            " Alice ",
            "ALICE",
            "%41lice",
            "%2541lice",
            "%252541LICE",
            "alice%20",
        ] {
            assert_eq!(of(Who::Caller(variant), None), plain, "{variant:?}");
        }
        assert_ne!(of(Who::Caller("alice2"), None), plain);
    }

    #[test]
    fn a_value_encoded_more_than_the_decode_limit_stays_a_different_bucket() {
        let deep = "%2525252541lice";
        assert_ne!(of(Who::Caller(deep), None), of(Who::Caller("alice"), None));
    }

    #[test]
    fn segments_kinds_and_tenants_cannot_be_confused() {
        let ip = of(Who::Client(Some("1.2.3.4")), None);
        assert_ne!(of(Who::Caller("1.2.3.4"), None), ip);
        assert_ne!(
            of(Who::Custom("1.2.3.4"), None),
            of(Who::Caller("1.2.3.4"), None)
        );
        let a = bucket(&KeyParts {
            scope: "ab",
            who: Who::Caller("c"),
            tenant: None,
        })
        .unwrap();
        let b = bucket(&KeyParts {
            scope: "a",
            who: Who::Caller("bc"),
            tenant: None,
        })
        .unwrap();
        assert_ne!(a.bucket, b.bucket);
        assert_ne!(
            of(Who::Caller("u"), Some("t1")),
            of(Who::Caller("u"), Some("t2"))
        );
        assert_ne!(of(Who::Caller("u"), Some("t1")), of(Who::Caller("u"), None));
    }

    #[test]
    fn addresses_collapse_to_their_network() {
        let v4 = of(Who::Client(Some("10.0.0.1")), None);
        assert_eq!(of(Who::Client(Some("::ffff:10.0.0.1")), None), v4);
        let net = of(Who::Client(Some("2001:db8:1:2::1")), None);
        assert_eq!(of(Who::Client(Some("2001:DB8:1:2:ffff::9")), None), net);
        assert_ne!(of(Who::Client(Some("2001:db8:1:3::1")), None), net);
    }

    #[test]
    fn an_unknown_client_is_flagged_and_oversized_segments_are_refused() {
        let parts = KeyParts {
            scope: "s",
            who: Who::Client(None),
            tenant: None,
        };
        assert!(bucket(&parts).unwrap().unknown);
        let long = "a".repeat(MAX_SEGMENT + 1);
        let parts = KeyParts {
            scope: "s",
            who: Who::Caller(&long),
            tenant: None,
        };
        assert_eq!(bucket(&parts), Err(KeyError::TooLong));
        assert_eq!(
            bucket(&KeyParts {
                scope: &long,
                who: Who::Caller("a"),
                tenant: None
            }),
            Err(KeyError::TooLong)
        );
    }

    #[test]
    fn the_hex_form_is_64_lowercase_digits() {
        let text = of(Who::Caller("a"), None).hex_string();
        assert_eq!(text.len(), 64);
        assert!(text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
    }
}
