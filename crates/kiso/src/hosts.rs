//! Host allow-list matching, equivalent to `hinoki.runtime.origins.HostPolicy`.

use std::net::{Ipv4Addr, Ipv6Addr};

/// An `exact` entry standing for the whole `127.0.0.0/8` block, which no
/// host name or suffix can express.
pub const LOOPBACK_V4: &[u8] = b"127.0.0.0/8";

#[derive(Debug, Clone, Default)]
pub struct Hosts {
    pub exact: Vec<Vec<u8>>,
    pub suffixes: Vec<Vec<u8>>,
}
impl Hosts {
    pub fn from_entries<S: AsRef<str>>(entries: &[S]) -> Self {
        let mut exact = Vec::new();
        let mut suffixes = Vec::new();
        for raw in entries {
            let entry = host_entry(raw.as_ref());
            if let Some(domain) = entry.strip_prefix('.') {
                exact.push(domain.as_bytes().to_vec());
                suffixes.push(entry.into_bytes());
            } else {
                exact.push(entry.into_bytes());
            }
        }
        Self { exact, suffixes }
    }

    pub fn allows(&self, header: &[u8]) -> bool {
        let Some(name) = host_name(header) else {
            return false;
        };
        self.exact
            .iter()
            .any(|host| host == &name || (host == LOOPBACK_V4 && is_ipv4_loopback(&name)))
            || self.suffixes.iter().any(|suffix| name.ends_with(suffix))
    }
}

fn is_ipv4_loopback(name: &[u8]) -> bool {
    std::str::from_utf8(name)
        .ok()
        .and_then(|text| text.parse::<Ipv4Addr>().ok())
        .is_some_and(|address| address.is_loopback())
}

pub fn host_entry(entry: &str) -> String {
    let lowered = entry.to_ascii_lowercase();
    if lowered.contains(':') {
        canonical_ipv6(lowered.as_bytes())
            .and_then(|bytes| String::from_utf8(bytes).ok())
            .unwrap_or_default()
    } else {
        lowered
    }
}

pub fn host_name(header: &[u8]) -> Option<Vec<u8>> {
    let header = trim_python_whitespace(header);
    if header.first() == Some(&b'[') {
        return bracketed_ipv6(header);
    }
    let name = without_port(header)?;
    let name = name.strip_suffix(b".").unwrap_or(name);
    let lowered: Vec<u8> = name.iter().map(u8::to_ascii_lowercase).collect();
    valid_name(&lowered).then_some(lowered)
}

fn bracketed_ipv6(header: &[u8]) -> Option<Vec<u8>> {
    let close = header.iter().position(|byte| *byte == b']')?;
    let rest = &header[close + 1..];
    if !rest.is_empty() && !(rest.first() == Some(&b':') && valid_port(&rest[1..])) {
        return None;
    }
    canonical_ipv6(&header[1..close])
}

fn without_port(header: &[u8]) -> Option<&[u8]> {
    let Some(colon) = header.iter().rposition(|byte| *byte == b':') else {
        return Some(header);
    };
    let host = &header[..colon];
    (host.iter().all(|byte| *byte != b':') && valid_port(&header[colon + 1..])).then_some(host)
}

fn valid_port(port: &[u8]) -> bool {
    !port.is_empty() && port.len() <= 5 && port.iter().all(u8::is_ascii_digit)
}

fn valid_name(name: &[u8]) -> bool {
    !name.is_empty() && name.split(|byte| *byte == b'.').all(valid_label)
}

fn valid_label(label: &[u8]) -> bool {
    let Some(first) = label.first() else {
        return false;
    };
    let last = label.last().copied().unwrap_or_default();
    is_name_char(*first)
        && is_name_char(last)
        && label
            .iter()
            .all(|byte| is_name_char(*byte) || *byte == b'-')
}

fn is_name_char(byte: u8) -> bool {
    byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'
}

fn canonical_ipv6(address: &[u8]) -> Option<Vec<u8>> {
    let text = std::str::from_utf8(address).ok()?;
    text.parse::<Ipv6Addr>()
        .ok()
        .map(|value| value.to_string().into_bytes())
}

fn trim_python_whitespace(value: &[u8]) -> &[u8] {
    let Ok(text) = std::str::from_utf8(value) else {
        return value;
    };
    text.trim_matches(is_python_whitespace).as_bytes()
}

fn is_python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}
