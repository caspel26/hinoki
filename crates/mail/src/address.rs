//! Email addresses: a bare `addr-spec` with an ASCII dot-atom local part and a
//! domain that is converted to ASCII (IDNA). Quoted local parts, comments,
//! address literals and non-ASCII local parts are refused.

use crate::error::Kind;

const MAX_LOCAL: usize = 64;
const MAX_DOMAIN: usize = 253;
const MAX_LABEL: usize = 63;
const MAX_ADDRESS: usize = 254;

/// A validated address in its normalized ASCII form (domain lower-cased and
/// converted to punycode, local part unchanged).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Address(String);

impl Address {
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The domain part.
    pub fn domain(&self) -> &str {
        self.0.rsplit_once('@').map_or("", |(_, domain)| domain)
    }
}

/// An address with an optional display name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mailbox {
    pub address: Address,
    pub name: Option<String>,
}

fn is_atext(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"!#$%&'*+/=?^_`{|}~-".contains(&byte)
}

fn valid_local(local: &str) -> bool {
    !local.is_empty()
        && local.len() <= MAX_LOCAL
        && local
            .split('.')
            .all(|atom| !atom.is_empty() && atom.bytes().all(is_atext))
}

fn valid_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    (1..=MAX_LABEL).contains(&bytes.len())
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'-')
        && bytes[0] != b'-'
        && bytes[bytes.len() - 1] != b'-'
}

/// The ASCII form of `domain`, or `None` when it is not a plain registered-name
/// domain of at least two labels.
fn ascii_domain(domain: &str) -> Option<String> {
    if domain.is_empty() || domain.len() > MAX_DOMAIN * 4 {
        return None;
    }
    let ascii = idna::domain_to_ascii_strict(domain)
        .ok()?
        .to_ascii_lowercase();
    let labels_ok = ascii.len() <= MAX_DOMAIN && ascii.split('.').all(valid_label);
    (labels_ok && ascii.contains('.')).then_some(ascii)
}

fn address_literal(text: &str) -> bool {
    let Some(inner) = text
        .strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
    else {
        return false;
    };
    match inner.strip_prefix("IPv6:") {
        Some(ip) => ip.parse::<std::net::Ipv6Addr>().is_ok(),
        None => inner.parse::<std::net::Ipv4Addr>().is_ok(),
    }
}

/// The normalized form of a domain used by the server settings (`helo_name`,
/// `message_id_domain`): a registered name converted to lower-case ASCII (a
/// single label such as `localhost` is fine), or a well-formed `[ipv4]` or
/// `[IPv6:ipv6]` literal. `None` for anything else.
pub fn normalize_domain(text: &str) -> Option<String> {
    if address_literal(text) {
        return Some(text.to_owned());
    }
    if text.is_empty() || text.len() > MAX_DOMAIN * 4 {
        return None;
    }
    let ascii = idna::domain_to_ascii_strict(text)
        .ok()?
        .to_ascii_lowercase();
    (ascii.len() <= MAX_DOMAIN && ascii.split('.').all(valid_label)).then_some(ascii)
}

/// Validate and normalize a bare address such as `ada@example.com`.
pub fn parse_addr_spec(input: &str) -> Result<Address, Kind> {
    let (local, domain) = input.split_once('@').ok_or(Kind::InvalidAddress)?;
    if domain.contains('@') || !valid_local(local) {
        return Err(Kind::InvalidAddress);
    }
    let domain = ascii_domain(domain).ok_or(Kind::InvalidAddress)?;
    let address = format!("{local}@{domain}");
    if address.len() > MAX_ADDRESS {
        return Err(Kind::InvalidAddress);
    }
    Ok(Address(address))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(input: &str) -> String {
        parse_addr_spec(input).unwrap().as_str().to_owned()
    }

    #[test]
    fn valid_addresses_are_normalized() {
        assert_eq!(ok("ada@example.com"), "ada@example.com");
        assert_eq!(
            ok("Ada.Lovelace+tag@Example.COM"),
            "Ada.Lovelace+tag@example.com"
        );
        assert_eq!(ok("o'brien_x@sub.example.org"), "o'brien_x@sub.example.org");
        assert_eq!(ok("ada@bücher.example"), "ada@xn--bcher-kva.example");
        assert_eq!(ok("a@b.c"), "a@b.c");
    }

    #[test]
    fn the_domain_is_reported_without_the_local_part() {
        let address = parse_addr_spec("ada@Example.com").unwrap();
        assert_eq!(address.domain(), "example.com");
    }

    #[test]
    fn malformed_addresses_are_refused() {
        let bad = [
            "",
            "ada",
            "@example.com",
            "ada@",
            "ada@@example.com",
            "a@b@example.com",
            "ada@example",
            "ada@example.com.",
            "ada@.example.com",
            "ada@exa..mple.com",
            "ada@-example.com",
            "ada@example-.com",
            "ada@exa mple.com",
            ".ada@example.com",
            "ada.@example.com",
            "ad..a@example.com",
            "\"ada\"@example.com",
            "ada(comment)@example.com",
            "ada@[127.0.0.1]",
            "Ada <ada@example.com>",
            "ada@example.com ",
            " ada@example.com",
            "ada@exam_ple.com",
            "ädä@example.com",
            "ada@example.com\r\nBcc: x@example.com",
            "ada\0@example.com",
        ];
        for input in bad {
            assert_eq!(
                parse_addr_spec(input),
                Err(Kind::InvalidAddress),
                "{input:?}"
            );
        }
    }

    #[test]
    fn length_limits_hold_at_the_edges() {
        let local = "a".repeat(64);
        assert!(parse_addr_spec(&format!("{local}@example.com")).is_ok());
        let local = "a".repeat(65);
        assert!(parse_addr_spec(&format!("{local}@example.com")).is_err());

        let label = "b".repeat(63);
        assert!(parse_addr_spec(&format!("a@{label}.com")).is_ok());
        let label = "b".repeat(64);
        assert!(parse_addr_spec(&format!("a@{label}.com")).is_err());

        let label = "c".repeat(63);
        let domain = format!("{label}.{label}.{label}.{}", "d".repeat(60));
        assert_eq!(domain.len(), 252);
        assert!(parse_addr_spec(&format!("a@{domain}")).is_ok());
        assert!(parse_addr_spec(&format!("a@{domain}e")).is_err());
        assert!(parse_addr_spec(&format!("{}@{domain}", "x".repeat(64))).is_err());
        assert!(parse_addr_spec(&format!("a@{}", "é".repeat(3000))).is_err());
    }
}
