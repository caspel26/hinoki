//! Trusted proxies: which socket peers may supply forwarded metadata, and the
//! one forwarded value the server itself reads (`X-Forwarded-Proto`). The same
//! rules as `hinoki.runtime.proxy`: a peer is trusted when its address lies in a
//! configured network of the same IP version (an IPv4-mapped IPv6 peer is an
//! IPv6 address and matches IPv6 networks only), and `X-Forwarded-Host` is never
//! read.

use std::net::IpAddr;

use crate::headers::Header;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Network {
    address: IpAddr,
    prefix: u8,
}

/// The configured proxy networks.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Networks {
    networks: Vec<Network>,
}

fn parse_entry(entry: &str) -> Result<Network, String> {
    let bad = || format!("invalid trusted proxy {entry:?}: expected an IP address or CIDR");
    let (host, prefix) = match entry.split_once('/') {
        Some((host, prefix)) => (host, Some(prefix)),
        None => (entry, None),
    };
    let address: IpAddr = host.parse().map_err(|_| bad())?;
    let widest: u8 = if address.is_ipv4() { 32 } else { 128 };
    let prefix = match prefix {
        None => widest,
        Some(text) if text.bytes().all(|b| b.is_ascii_digit()) && !text.is_empty() => {
            text.parse::<u8>().map_err(|_| bad())?
        }
        Some(_) => return Err(bad()),
    };
    if prefix > widest {
        return Err(bad());
    }
    Ok(Network { address, prefix })
}

/// The leading `prefix` bits of both addresses agree.
fn same_prefix(left: &[u8], right: &[u8], prefix: u8) -> bool {
    let whole = usize::from(prefix / 8);
    let rest = prefix % 8;
    if left[..whole] != right[..whole] {
        return false;
    }
    rest == 0 || (left[whole] ^ right[whole]) >> (8 - rest) == 0
}

impl Network {
    fn contains(&self, peer: IpAddr) -> bool {
        match (self.address, peer) {
            (IpAddr::V4(net), IpAddr::V4(ip)) => {
                same_prefix(&net.octets(), &ip.octets(), self.prefix)
            }
            (IpAddr::V6(net), IpAddr::V6(ip)) => {
                same_prefix(&net.octets(), &ip.octets(), self.prefix)
            }
            _ => false,
        }
    }
}

impl Networks {
    /// Parse addresses and CIDRs (host bits may be set, as with Python's
    /// `ip_network(strict=False)`); the first invalid entry is an error.
    pub fn parse<S: AsRef<str>>(entries: &[S]) -> Result<Self, String> {
        let networks = entries
            .iter()
            .map(|entry| parse_entry(entry.as_ref()))
            .collect::<Result<_, _>>()?;
        Ok(Self { networks })
    }

    pub fn is_empty(&self) -> bool {
        self.networks.is_empty()
    }

    /// Whether `peer` belongs to a configured network.
    pub fn contains(&self, peer: IpAddr) -> bool {
        self.networks.iter().any(|network| network.contains(peer))
    }
}

/// The scheme a single `x-forwarded-proto` header names (after trimming and
/// ignoring case): `Some(true)` for `https`, `Some(false)` for `http`. Any other
/// value, or a repeated header, names nothing and the socket scheme stands.
/// Names in `headers` are lowercase.
pub fn forwarded_scheme(headers: &[Header]) -> Option<bool> {
    let mut found = headers
        .iter()
        .filter(|(name, _)| name == b"x-forwarded-proto");
    let (Some((_, value)), None) = (found.next(), found.next()) else {
        return None;
    };
    let text = std::str::from_utf8(value).ok()?.trim_matches([' ', '\t']);
    if text.eq_ignore_ascii_case("https") {
        Some(true)
    } else if text.eq_ignore_ascii_case("http") {
        Some(false)
    } else {
        None
    }
}

/// `true` when [`forwarded_scheme`] says https.
pub fn forwarded_https(headers: &[Header]) -> bool {
    forwarded_scheme(headers) == Some(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nets(entries: &[&str]) -> Networks {
        Networks::parse(entries).unwrap()
    }

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    #[test]
    fn ipv4_networks_and_addresses() {
        let proxies = nets(&["10.0.0.0/8", "192.168.1.7"]);
        assert!(proxies.contains(ip("10.255.255.255")));
        assert!(!proxies.contains(ip("11.0.0.1")));
        assert!(proxies.contains(ip("192.168.1.7")));
        assert!(!proxies.contains(ip("192.168.1.8")));
    }

    #[test]
    fn partial_byte_prefixes() {
        let proxies = nets(&["10.20.0.0/15"]);
        assert!(proxies.contains(ip("10.21.9.9")));
        assert!(!proxies.contains(ip("10.22.0.1")));
    }

    #[test]
    fn host_bits_are_allowed() {
        assert!(nets(&["10.1.2.3/8"]).contains(ip("10.200.0.1")));
    }

    #[test]
    fn ipv6_networks_and_addresses() {
        let proxies = nets(&["2001:db8::/32", "fd00::1"]);
        assert!(proxies.contains(ip("2001:db8:ffff::1")));
        assert!(!proxies.contains(ip("2001:db9::1")));
        assert!(proxies.contains(ip("fd00::1")));
        assert!(!proxies.contains(ip("fd00::2")));
    }

    #[test]
    fn the_versions_never_cross() {
        assert!(!nets(&["0.0.0.0/0"]).contains(ip("::1")));
        assert!(!nets(&["::/0"]).contains(ip("127.0.0.1")));
    }

    #[test]
    fn an_ipv4_mapped_peer_is_an_ipv6_address() {
        assert!(!nets(&["10.0.0.0/8"]).contains(ip("::ffff:10.0.0.1")));
        assert!(nets(&["::ffff:10.0.0.0/104"]).contains(ip("::ffff:10.0.0.1")));
        assert!(nets(&["::/0"]).contains(ip("::ffff:10.0.0.1")));
    }

    #[test]
    fn zero_and_full_prefixes() {
        let all = nets(&["0.0.0.0/0"]);
        assert!(all.contains(ip("203.0.113.9")));
        let one = nets(&["203.0.113.9/32"]);
        assert!(one.contains(ip("203.0.113.9")));
        assert!(!one.contains(ip("203.0.113.10")));
        assert!(nets(&["2001:db8::1/128"]).contains(ip("2001:db8::1")));
        assert!(!nets(&["2001:db8::1/128"]).contains(ip("2001:db8::2")));
    }

    #[test]
    fn an_empty_list_trusts_nobody() {
        let none = Networks::parse::<&str>(&[]).unwrap();
        assert!(none.is_empty());
        assert!(!none.contains(ip("127.0.0.1")));
    }

    #[test]
    fn invalid_entries_are_refused() {
        for bad in [
            "",
            "nope",
            "10.0.0.0/33",
            "::/129",
            "10.0.0.0/",
            "10.0.0.0/-1",
            "10.0.0.0/8/8",
            "10.0.0.0/+8",
            "10.0.0.0/255.0.0.0",
            " 10.0.0.1",
            "10.0.0.256",
        ] {
            let err = Networks::parse(&["127.0.0.1", bad]).unwrap_err();
            assert!(err.contains(&format!("{bad:?}")), "{bad:?}: {err}");
        }
    }

    fn h(name: &str, value: &str) -> Header {
        (name.as_bytes().to_vec(), value.as_bytes().to_vec())
    }

    #[test]
    fn forwarded_scheme_tells_http_from_nothing() {
        assert_eq!(
            forwarded_scheme(&[h("x-forwarded-proto", "http")]),
            Some(false)
        );
        assert_eq!(
            forwarded_scheme(&[h("x-forwarded-proto", "HTTPS")]),
            Some(true)
        );
        assert_eq!(forwarded_scheme(&[h("x-forwarded-proto", "ws")]), None);
        assert_eq!(forwarded_scheme(&[]), None);
    }

    #[test]
    fn forwarded_proto_needs_one_valid_https_value() {
        assert!(forwarded_https(&[h("x-forwarded-proto", "https")]));
        assert!(forwarded_https(&[h("x-forwarded-proto", " HTTPS\t")]));
        assert!(!forwarded_https(&[h("x-forwarded-proto", "http")]));
        assert!(!forwarded_https(&[h("x-forwarded-proto", "wss")]));
        assert!(!forwarded_https(&[h("x-forwarded-proto", "https, http")]));
        assert!(!forwarded_https(&[]));
        assert!(!forwarded_https(&[
            h("x-forwarded-proto", "https"),
            h("x-forwarded-proto", "https"),
        ]));
        assert!(!forwarded_https(&[h("x-forwarded-host", "https")]));
    }
}
