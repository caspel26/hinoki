//! Wire forms of the `ipaddress` field kinds, shared by both validation paths.
//!
//! The grammar is the standard library's, minus what Hinoki does not take:
//! an IPv6 zone id (`fe80::1%eth0`), a network written with a netmask
//! (`10.0.0.0/255.0.0.0`) and any prefix that is not plain ASCII digits. A
//! network must have no host bits set, and a bare address is a single-host
//! network.

use std::net::{Ipv4Addr, Ipv6Addr};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum NetKind {
    V4,
    V6,
    V4Network,
    V6Network,
}

impl NetKind {
    /// The error code, which is also the field kind's name.
    pub(crate) fn code(self) -> &'static str {
        match self {
            Self::V4 => "ipv4",
            Self::V6 => "ipv6",
            Self::V4Network => "ipv4network",
            Self::V6Network => "ipv6network",
        }
    }

    pub(crate) fn message(self) -> &'static str {
        match self {
            Self::V4 => "expected an IPv4 address",
            Self::V6 => "expected an IPv6 address",
            Self::V4Network => "expected an IPv4 network with no host bits",
            Self::V6Network => "expected an IPv6 network with no host bits",
        }
    }

    /// The `ipaddress` class a valid text becomes.
    pub(crate) fn class_name(self) -> &'static str {
        match self {
            Self::V4 => "IPv4Address",
            Self::V6 => "IPv6Address",
            Self::V4Network => "IPv4Network",
            Self::V6Network => "IPv6Network",
        }
    }

    pub(crate) fn accepts(self, text: &str) -> bool {
        match self {
            Self::V4 => text.parse::<Ipv4Addr>().is_ok(),
            Self::V6 => text.parse::<Ipv6Addr>().is_ok(),
            Self::V4Network => network_has_no_host_bits(text, 32, |addr| {
                addr.parse::<Ipv4Addr>()
                    .ok()
                    .map(|a| u128::from(u32::from(a)))
            }),
            Self::V6Network => network_has_no_host_bits(text, 128, |addr| {
                addr.parse::<Ipv6Addr>().ok().map(u128::from)
            }),
        }
    }
}

/// `address[/prefix]` whose address has no bit set beyond the prefix.
fn network_has_no_host_bits(text: &str, width: u32, parse: impl Fn(&str) -> Option<u128>) -> bool {
    let (address, prefix) = match text.split_once('/') {
        Some((address, prefix)) => (address, Some(prefix)),
        None => (text, None),
    };
    let prefix = match prefix {
        None => width,
        Some(digits) if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) => {
            match digits.parse::<u32>() {
                Ok(length) if length <= width => length,
                _ => return false,
            }
        }
        Some(_) => return false,
    };
    let Some(bits) = parse(address) else {
        return false;
    };
    let host_mask = if prefix == width {
        0
    } else {
        u128::MAX >> (128 - (width - prefix))
    };
    bits & host_mask == 0
}

#[cfg(test)]
mod tests {
    use super::NetKind;

    #[test]
    fn networks_need_zero_host_bits() {
        assert!(NetKind::V4Network.accepts("10.0.0.0/8"));
        assert!(NetKind::V4Network.accepts("10.1.2.3"));
        assert!(NetKind::V4Network.accepts("0.0.0.0/0"));
        assert!(!NetKind::V4Network.accepts("10.0.0.1/8"));
        assert!(!NetKind::V4Network.accepts("10.0.0.0/255.0.0.0"));
        assert!(NetKind::V6Network.accepts("::/0"));
        assert!(NetKind::V6Network.accepts("2001:db8::/32"));
        assert!(!NetKind::V6Network.accepts("2001:db8::1/32"));
        assert!(!NetKind::V6Network.accepts("::/129"));
    }

    #[test]
    fn zone_ids_and_signs_are_refused() {
        assert!(!NetKind::V6.accepts("fe80::1%eth0"));
        assert!(!NetKind::V4Network.accepts("10.0.0.0/+8"));
        assert!(!NetKind::V4.accepts("01.2.3.4"));
    }
}
