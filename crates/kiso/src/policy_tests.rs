use crate::cors::{Cors, PreflightError};
use crate::hosts::Hosts;
use crate::redirect;

fn hosts(exact: &[&str], suffixes: &[&str]) -> Hosts {
    Hosts {
        exact: exact.iter().map(|host| host.as_bytes().to_vec()).collect(),
        suffixes: suffixes
            .iter()
            .map(|host| host.as_bytes().to_vec())
            .collect(),
    }
}

fn listed_cors() -> Cors {
    Cors {
        origins: vec![b"https://a.example".to_vec()],
        credentials: true,
        expose: b"x-total".to_vec(),
        preflight: Vec::new(),
        ..Cors::default()
    }
}

#[test]
fn host_names_match_the_python_policy_shapes() {
    let allowed = hosts(
        &["example.com", "127.0.0.1", "2001:db8::1", "example.org"],
        &[".example.org"],
    );
    for host in [
        "EXAMPLE.COM:443",
        "example.com.",
        "example.org",
        "a.example.org",
    ] {
        assert!(allowed.allows(host.as_bytes()), "{host}");
    }
    assert!(allowed.allows(b"[2001:0db8::1]:443"));
    assert!(allowed.allows(b" 127.0.0.1:00000 "));
    for host in [
        "",
        "example.com.evil",
        "aexample.org",
        "example.com:",
        "[::1",
    ] {
        assert!(!allowed.allows(host.as_bytes()), "{host}");
    }
}

#[test]
fn the_loopback_entry_matches_every_ipv4_loopback_address_only() {
    let allowed = hosts(&["localhost", "::1", "127.0.0.0/8"], &[".localhost"]);
    for host in [
        "127.0.0.1",
        "127.0.0.1:8000",
        "127.255.255.254",
        "LOCALHOST:8000",
        "dev.localhost",
        "[::1]:8000",
        "localhost.",
    ] {
        assert!(allowed.allows(host.as_bytes()), "{host}");
    }
    for host in [
        "",
        "128.0.0.1",
        "192.168.1.5",
        "127.0.0",
        "127.0.0.1.evil.example",
        "evil.example",
        "evil-localhost",
        "[::2]",
        "127.0.0.0/8",
    ] {
        assert!(!allowed.allows(host.as_bytes()), "{host}");
    }
    assert!(!hosts(&["example.com"], &[]).allows(b"127.0.0.1"));
}

#[test]
fn repeated_host_headers_never_match_one_allowed_host() {
    assert!(!hosts(&["example.com"], &[]).allows(b"example.com, example.com"));
}

#[test]
fn trims_latin1_whitespace_after_header_decoding() {
    assert!(hosts(&["example.com"], &[]).allows("\u{a0}example.com\u{a0}".as_bytes()));
    assert!(hosts(&["example.com"], &[]).allows("\u{85}example.com\u{85}".as_bytes()));
}

#[test]
fn listed_origin_gets_permission_and_vary() {
    let names: Vec<_> = listed_cors()
        .headers(b"https://a.example")
        .into_iter()
        .map(|(name, _)| String::from_utf8(name).unwrap())
        .collect();
    assert_eq!(
        names,
        [
            "access-control-allow-origin",
            "vary",
            "access-control-allow-credentials",
            "access-control-expose-headers"
        ]
    );
}

#[test]
fn unlisted_origin_only_varies() {
    assert_eq!(
        listed_cors().headers(b"https://evil.example"),
        vec![(b"vary".to_vec(), b"origin".to_vec())]
    );
}

#[test]
fn any_origin_answers_only_when_an_origin_was_sent() {
    let any = Cors {
        any_origin: true,
        ..Cors::default()
    };
    assert!(any.headers(b"").is_empty());
    assert_eq!(any.headers(b"https://x").len(), 1);
}

#[test]
fn preflight_requires_an_origin_and_rejects_unlisted_origins() {
    let cors = listed_cors();
    assert_eq!(cors.preflight("OPTIONS", None, true), Ok(None));
    assert_eq!(
        cors.preflight("GET", Some(b"https://a.example"), true),
        Ok(None)
    );
    assert_eq!(
        cors.preflight("OPTIONS", Some(b"https://evil.example"), true),
        Err(PreflightError::OriginDenied)
    );
}

#[test]
fn redirect_preserves_raw_path_and_query_and_removes_port_80() {
    assert_eq!(
        redirect::location(b"example.com:80", b"/a%2Fb c", b"q=%FF&x=hello world"),
        Some(b"https://example.com/a%2Fb%20c?q=%FF&x=hello%20world".to_vec())
    );
}

#[test]
fn redirect_refuses_untrusted_header_bytes() {
    assert_eq!(redirect::location(b"evil\r\nlocation:x", b"/", b""), None);
    assert_eq!(redirect::location(b"", b"/", b""), None);
}

#[test]
fn redirect_skips_a_target_that_is_not_a_path() {
    assert_eq!(redirect::location(b"example.com", b"*", b""), None);
    assert_eq!(
        redirect::location(b"example.com", b"", b""),
        Some(b"https://example.com/".to_vec())
    );
}
