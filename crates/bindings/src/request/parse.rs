use std::collections::HashMap;

pub(super) fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Percent-decode `raw` (`+` is a space when `plus`); `None` if the result is
/// not UTF-8. A `%` that is not followed by two hex digits stays literal.
fn unquote(raw: &[u8], plus: bool) -> Option<String> {
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        match raw[i] {
            b'+' if plus => out.push(b' '),
            b'%' if i + 2 < raw.len() && hex(raw[i + 1]).is_some() && hex(raw[i + 2]).is_some() => {
                out.push(hex(raw[i + 1])? * 16 + hex(raw[i + 2])?);
                i += 2;
            }
            byte => out.push(byte),
        }
        i += 1;
    }
    String::from_utf8(out).ok()
}

/// Decode one router-matched path parameter. A plus remains a plus and a
/// percent-encoded slash stays within the segment selected by the router.
pub fn decode_path_parameter(raw: &str) -> Option<String> {
    unquote(raw.as_bytes(), false)
}

/// `a=1&b=2&a=3` into ordered pairs. Blank values are kept, empty pairs are
/// skipped, the input must be ASCII, every decoded part valid UTF-8 and there
/// are at most `max_pairs` pairs.
pub fn parse_query(raw: &[u8], max_pairs: usize) -> Result<Vec<(String, String)>, &'static str> {
    if !raw.is_ascii() {
        return Err("query string is not ASCII");
    }
    let mut pairs = Vec::new();
    for part in raw.split(|&b| b == b'&').filter(|p| !p.is_empty()) {
        if pairs.len() >= max_pairs {
            return Err("too many query or form fields");
        }
        let (name, value) = match part.iter().position(|&b| b == b'=') {
            Some(at) => (&part[..at], &part[at + 1..]),
            None => (part, &part[..0]),
        };
        let name = unquote(name, true).ok_or("query string is not valid UTF-8")?;
        let value = unquote(value, true).ok_or("query string is not valid UTF-8")?;
        pairs.push((name, value));
    }
    Ok(pairs)
}

/// `Cookie` header into a map; the first value of a repeated name wins and
/// surrounding quotes are stripped. Malformed parts are ignored.
pub fn parse_cookies(header: &str) -> Vec<(String, String)> {
    let mut seen = HashMap::new();
    let mut cookies = Vec::new();
    for part in header.split(';') {
        let Some((name, value)) = part.trim().split_once('=') else {
            continue;
        };
        if name.is_empty() || seen.contains_key(name) {
            continue;
        }
        seen.insert(name.to_owned(), ());
        let value = value.trim();
        let value = value
            .strip_prefix('"')
            .and_then(|v| v.strip_suffix('"'))
            .unwrap_or(value);
        cookies.push((name.to_owned(), value.to_owned()));
    }
    cookies
}

/// Lower-case header names with repeated headers joined by `", "`. Header
/// bytes are Latin-1, as in ASGI.
pub fn join_headers<N: AsRef<[u8]>, V: AsRef<[u8]>>(headers: &[(N, V)]) -> Vec<(String, String)> {
    let mut joined: Vec<(String, String)> = Vec::with_capacity(headers.len());
    let mut index: HashMap<String, usize> = HashMap::with_capacity(headers.len());
    for (name, value) in headers {
        let name = latin1(name.as_ref()).to_lowercase();
        let value = latin1(value.as_ref());
        match index.get(&name) {
            Some(&at) => {
                // HTTP/2 splits Cookie into several fields: rejoin with "; ".
                joined[at]
                    .1
                    .push_str(if name == "cookie" { "; " } else { ", " });
                joined[at].1.push_str(&value);
            }
            None => {
                index.insert(name.clone(), joined.len());
                joined.push((name, value));
            }
        }
    }
    joined
}

/// Look up one lower-case header name without building the full header map.
/// Matches `join_headers` semantics: repeated values are joined with `"; "`
/// for `cookie` and `", "` otherwise, and any non-lower-case lookup key returns
/// `None`.
pub fn lookup_header<N: AsRef<[u8]>, V: AsRef<[u8]>>(
    headers: &[(N, V)],
    name: &str,
) -> Option<String> {
    if name.chars().any(char::is_uppercase) {
        return None;
    }
    if !name.is_ascii()
        || !headers
            .iter()
            .all(|(k, v)| k.as_ref().is_ascii() && v.as_ref().is_ascii())
    {
        return join_headers(headers)
            .into_iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v);
    }
    lookup_ascii_header(headers, name)
}

fn lookup_ascii_header<N: AsRef<[u8]>, V: AsRef<[u8]>>(
    headers: &[(N, V)],
    name: &str,
) -> Option<String> {
    let target = name.as_bytes();
    let separator = if name == "cookie" { "; " } else { ", " };
    let mut joined: Option<String> = None;
    for (h_name, h_val) in headers {
        if !h_name.as_ref().eq_ignore_ascii_case(target) {
            continue;
        }
        let text = std::str::from_utf8(h_val.as_ref()).ok()?;
        match &mut joined {
            Some(existing) => {
                existing.push_str(separator);
                existing.push_str(text);
            }
            None => joined = Some(text.to_owned()),
        }
    }
    joined
}

/// Latin-1 bytes as a `String`; plain ASCII (every real header) skips the
/// per-byte widening.
fn latin1(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(text) if text.is_ascii() => text.to_owned(),
        _ => bytes.iter().map(|&b| char::from(b)).collect(),
    }
}

/// Pick the best of `available` for an `Accept` header (RFC 9110 §12.5.1):
/// highest quality, then most specific matching range, then earliest in
/// `available`. `None` when nothing is acceptable; an empty header accepts
/// anything, so the first entry wins.
pub fn negotiate(accept: &str, available: &[String]) -> Option<usize> {
    if accept.trim().is_empty() {
        return if available.is_empty() { None } else { Some(0) };
    }
    let ranges = parse_accept_ranges(accept);
    let mut best: Option<(f32, u8, usize)> = None;
    for (index, offered) in available.iter().enumerate() {
        let offered = normalize_offered(offered);
        if let Some((specificity, quality)) = best_range(&ranges, &offered) {
            if quality > 0.0 && is_better_match(best, quality, specificity) {
                best = Some((quality, specificity, index));
            }
        }
    }
    best.map(|(_, _, index)| index)
}

fn parse_accept_ranges(accept: &str) -> Vec<(String, f32)> {
    accept
        .split(',')
        .filter_map(|item| {
            let mut pieces = item.split(';');
            let media = pieces.next()?.trim().to_lowercase();
            if media.is_empty() {
                return None;
            }
            let mut quality = 1.0_f32;
            for param in pieces {
                if let Some((name, value)) = param.split_once('=') {
                    if name.trim().eq_ignore_ascii_case("q") {
                        quality = value.trim().parse::<f32>().ok()?.clamp(0.0, 1.0);
                    }
                }
            }
            Some((media, quality))
        })
        .collect()
}

fn normalize_offered(offered: &str) -> String {
    offered
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

fn best_range(ranges: &[(String, f32)], offered: &str) -> Option<(u8, f32)> {
    let group = offered.split('/').next().unwrap_or("");
    ranges
        .iter()
        .filter_map(|(media, quality)| {
            let specificity = media_specificity(media, offered, group)?;
            Some((specificity, *quality))
        })
        .fold(None::<(u8, f32)>, |best, found| match best {
            Some((spec, _)) if spec >= found.0 => best, // first range wins a tie
            _ => Some(found),
        })
}

fn media_specificity(media: &str, offered: &str, group: &str) -> Option<u8> {
    if media == offered {
        Some(3)
    } else if media.strip_suffix("/*") == Some(group) {
        Some(2)
    } else if media == "*/*" {
        Some(1)
    } else {
        None
    }
}

fn is_better_match(best: Option<(f32, u8, usize)>, quality: f32, specificity: u8) -> bool {
    match best {
        None => true,
        Some((previous_quality, previous_specificity, _)) => {
            quality > previous_quality
                || (quality == previous_quality && specificity > previous_specificity)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(a: &str, b: &str) -> (String, String) {
        (a.to_owned(), b.to_owned())
    }

    fn offered(types: &[&str]) -> Vec<String> {
        types.iter().map(|t| (*t).to_owned()).collect()
    }

    #[test]
    fn query_keeps_blank_values_decodes_and_orders() {
        let got = parse_query(b"a=1&b&c=&a=x%20y+z&&d=%zz", 100).unwrap();
        assert_eq!(
            got,
            vec![
                pair("a", "1"),
                pair("b", ""),
                pair("c", ""),
                pair("a", "x y z"),
                pair("d", "%zz")
            ]
        );
    }

    #[test]
    fn path_parameters_decode_without_form_query_rules() {
        assert_eq!(decode_path_parameter("a%2Fb+c"), Some("a/b+c".into()));
        assert_eq!(decode_path_parameter("bad%ff"), None);
    }

    #[test]
    fn query_rejects_non_ascii_and_bad_utf8() {
        assert!(parse_query("é=1".as_bytes(), 9).is_err());
        assert!(parse_query(b"a=%ff", 9).is_err());
        assert!(parse_query(b"%ff=1", 9).is_err());
    }

    #[test]
    fn a_trailing_percent_stays_literal() {
        assert_eq!(parse_query(b"a=%4", 9).unwrap(), vec![pair("a", "%4")]);
        assert_eq!(parse_query(b"a=%41", 9).unwrap(), vec![pair("a", "A")]);
    }

    #[test]
    fn cookies_first_wins_and_junk_is_ignored() {
        let got = parse_cookies("a=1; b=\"two\"; a=3; junk; =x");
        assert_eq!(got, vec![pair("a", "1"), pair("b", "two")]);
    }

    #[test]
    fn repeated_headers_are_joined_in_order() {
        let got = join_headers(&[
            (b"X-A".to_vec(), b"1".to_vec()),
            (b"x-b".to_vec(), b"2".to_vec()),
            (b"x-a".to_vec(), b"3".to_vec()),
        ]);
        assert_eq!(got, vec![pair("x-a", "1, 3"), pair("x-b", "2")]);
    }

    #[test]
    fn accept_prefers_quality_then_specificity_then_order() {
        let both = offered(&["application/json", "text/csv"]);
        assert_eq!(negotiate("", &both), Some(0));
        assert_eq!(negotiate("*/*", &both), Some(0));
        assert_eq!(negotiate("text/csv", &both), Some(1));
        assert_eq!(
            negotiate("text/*;q=0.5, application/json;q=0.4", &both),
            Some(1)
        );
        assert_eq!(negotiate("*/*;q=0.1, text/csv", &both), Some(1));
        assert_eq!(negotiate("application/*, text/csv", &both), Some(1));
        assert_eq!(negotiate("TEXT/CSV ; Q=0.9", &both), Some(1));
    }

    #[test]
    fn accept_refuses_what_it_excludes() {
        let both = offered(&["application/json", "text/csv"]);
        assert_eq!(negotiate("text/html", &both), None);
        assert_eq!(negotiate("application/json;q=0", &both), None);
        assert_eq!(negotiate("*/*, application/json;q=0", &both), Some(1));
        assert_eq!(
            negotiate("application/json;q=abc, text/csv", &both),
            Some(1)
        );
        assert_eq!(negotiate("", &[]), None);
    }
}
