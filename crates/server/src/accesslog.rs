//! One access-log line per answered request, written by the transport.
//!
//! The line is built from what the transport knows (peer, request line, status,
//! the time the backend took to decide the answer) and printed to stderr without
//! the GIL, so an answer the plan gives early, one the lane serves and one that
//! went through Python are all logged the same way. The time excludes writing the
//! response to the client.

use std::fmt::Write as _;
use std::io::Write as _;
use std::net::SocketAddr;
use std::time::Duration;

use hyper::StatusCode;

/// How a line looks: `Plain` is one greppable line, `Pretty` adds the level tag,
/// colours and alignment of a development console.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessLog {
    Plain,
    Pretty,
}

/// What one line says about a request.
pub struct Entry<'a> {
    pub peer: SocketAddr,
    pub method: &'a str,
    pub path: &'a str,
    pub query: &'a [u8],
    pub http_version: &'a str,
    pub status: u16,
    pub elapsed: Duration,
}

const RESET: &str = "\x1b[0m";
const BOLD: &str = "\x1b[1m";
const DIM: &str = "\x1b[2m";
const GREEN: &str = "\x1b[32m";
const CYAN: &str = "\x1b[36m";
const YELLOW: &str = "\x1b[33m";
const RED: &str = "\x1b[31m";

fn status_colour(status: u16) -> &'static str {
    match status {
        100..=299 => GREEN,
        300..=399 => CYAN,
        400..=499 => YELLOW,
        _ => RED,
    }
}

/// Request-target text with every control character shown as `\xNN`, so a
/// client cannot forge a log line.
fn printable(text: &str, out: &mut String) {
    for c in text.chars() {
        if c.is_control() {
            let _ = write!(out, "\\x{:02x}", c as u32);
        } else {
            out.push(c);
        }
    }
}

fn duration_text(elapsed: Duration) -> String {
    let micros = elapsed.as_micros();
    if micros < 1_000 {
        format!("{micros}µs")
    } else if micros < 1_000_000 {
        format!("{:.1}ms", micros as f64 / 1_000.0)
    } else {
        format!("{:.2}s", micros as f64 / 1_000_000.0)
    }
}

fn target_of(entry: &Entry<'_>) -> String {
    let mut target = String::with_capacity(entry.path.len() + entry.query.len() + 1);
    printable(entry.path, &mut target);
    if !entry.query.is_empty() {
        target.push('?');
        printable(&String::from_utf8_lossy(entry.query), &mut target);
    }
    target
}

fn reason_of(status: u16) -> &'static str {
    StatusCode::from_u16(status)
        .ok()
        .and_then(|code| code.canonical_reason())
        .unwrap_or("")
}

/// The line for `entry`, without the trailing newline.
pub fn line(style: AccessLog, entry: &Entry<'_>) -> String {
    let target = target_of(entry);
    let reason = reason_of(entry.status);
    let took = duration_text(entry.elapsed);
    let request = format!(
        "\"{} {} HTTP/{}\"",
        entry.method, target, entry.http_version
    );
    match style {
        AccessLog::Plain => format!(
            "{} - {} {} {} {}",
            entry.peer, request, entry.status, reason, took
        ),
        AccessLog::Pretty => format!(
            "{GREEN}INFO{RESET}:     {} - {BOLD}{request}{RESET} {}{} {reason}{RESET}  {DIM}{took}{RESET}",
            entry.peer,
            status_colour(entry.status),
            entry.status,
        ),
    }
}

/// Print the line for `entry`. A closed or failing stderr is ignored: logging
/// never decides an answer.
pub fn log(style: AccessLog, entry: &Entry<'_>) {
    let mut text = line(style, entry);
    text.push('\n');
    let _ = std::io::stderr().lock().write_all(text.as_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry<'a>(status: u16, query: &'a [u8], path: &'a str) -> Entry<'a> {
        Entry {
            peer: "127.0.0.1:52341".parse().unwrap(),
            method: "GET",
            path,
            query,
            http_version: "1.1",
            status,
            elapsed: Duration::from_micros(3_200),
        }
    }

    #[test]
    fn plain_line_is_one_greppable_line() {
        let text = line(AccessLog::Plain, &entry(200, b"limit=5", "/items"));
        assert_eq!(
            text,
            "127.0.0.1:52341 - \"GET /items?limit=5 HTTP/1.1\" 200 OK 3.2ms"
        );
    }

    #[test]
    fn pretty_line_colours_the_status_by_class() {
        let ok = line(AccessLog::Pretty, &entry(200, b"", "/"));
        assert!(ok.contains("\x1b[32m200 OK\x1b[0m"));
        assert!(ok.starts_with("\x1b[32mINFO\x1b[0m:     127.0.0.1:52341"));
        assert!(line(AccessLog::Pretty, &entry(404, b"", "/")).contains("\x1b[33m404 Not Found"));
        assert!(line(AccessLog::Pretty, &entry(503, b"", "/")).contains("\x1b[31m503"));
        assert!(line(AccessLog::Pretty, &entry(302, b"", "/")).contains("\x1b[36m302"));
    }

    #[test]
    fn control_characters_cannot_forge_a_line() {
        let text = line(AccessLog::Plain, &entry(200, b"a=%0A", "/x\ny"));
        assert!(!text.contains('\n'));
        assert!(text.contains("/x\\x0ay"));
    }

    #[test]
    fn durations_use_a_readable_unit() {
        assert_eq!(duration_text(Duration::from_micros(812)), "812µs");
        assert_eq!(duration_text(Duration::from_micros(3_200)), "3.2ms");
        assert_eq!(duration_text(Duration::from_millis(1_500)), "1.50s");
    }
}
