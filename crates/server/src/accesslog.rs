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
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hyper::StatusCode;

/// How a line looks: `Plain` is one greppable line, `Pretty` adds the level tag,
/// colours and alignment of a development console, `Json` is one JSON object
/// per line for a log pipeline. `Logger` renders the JSON object too, but hands
/// it to a [`Sink`] instead of writing it to stderr.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessLog {
    Plain,
    Pretty,
    Json,
    Logger,
}

/// What one line says about a request.
/// Where a request was served, as far as the log can tell: answered by the
/// transport or the classifier (`Early`), handed to Python, or run natively.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lane {
    Early,
    Python,
    Native,
}

impl Lane {
    fn name(self) -> &'static str {
        match self {
            Self::Early => "early",
            Self::Python => "python",
            Self::Native => "native",
        }
    }
}

/// Written by the backend while it handles a request, read by the log line.
#[derive(Debug, Default)]
pub struct Trace(AtomicU8);

impl Trace {
    pub fn set(&self, lane: Lane) {
        self.0.store(lane as u8 + 1, Ordering::Relaxed);
    }

    pub fn lane(&self) -> Option<Lane> {
        match self.0.load(Ordering::Relaxed) {
            1 => Some(Lane::Early),
            2 => Some(Lane::Python),
            3 => Some(Lane::Native),
            _ => None,
        }
    }
}

pub struct Entry<'a> {
    pub peer: SocketAddr,
    pub method: &'a str,
    pub path: &'a str,
    pub query: &'a [u8],
    pub http_version: &'a str,
    pub status: u16,
    pub elapsed: Duration,
    pub lane: Option<Lane>,
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
    line_at(style, entry, SystemTime::now())
}

/// Days since 1970-01-01 as (year, month, day), proleptic Gregorian (Hinnant's
/// `civil_from_days`).
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// RFC 3339 UTC with millisecond precision, e.g. `2026-11-19T19:56:25.881Z`.
fn timestamp(at: SystemTime) -> String {
    let since = at.duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = since.as_secs() as i64;
    let (year, month, day) = civil_from_days(secs.div_euclid(86_400));
    let rest = secs.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rest / 3_600,
        rest % 3_600 / 60,
        rest % 60,
        since.subsec_millis()
    )
}

/// JSON string content: quotes, backslashes and control characters escaped.
fn json_escaped(text: &str, out: &mut String) {
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if c.is_control() => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
}

fn json_line(entry: &Entry<'_>, at: SystemTime) -> String {
    let mut text = String::with_capacity(192);
    let _ = write!(
        text,
        "{{\"time\":\"{}\",\"peer\":\"{}\",\"method\":\"",
        timestamp(at),
        entry.peer
    );
    json_escaped(entry.method, &mut text);
    text.push_str("\",\"path\":\"");
    json_escaped(entry.path, &mut text);
    text.push_str("\",\"query\":\"");
    json_escaped(&String::from_utf8_lossy(entry.query), &mut text);
    let _ = write!(
        text,
        "\",\"http_version\":\"{}\",\"status\":{},\"duration_ms\":{:.3}",
        entry.http_version,
        entry.status,
        entry.elapsed.as_secs_f64() * 1_000.0
    );
    if let Some(lane) = entry.lane {
        let _ = write!(text, ",\"lane\":\"{}\"", lane.name());
    }
    text.push('}');
    text
}

/// `line` with the time of the request given, so it can be tested.
pub fn line_at(style: AccessLog, entry: &Entry<'_>, at: SystemTime) -> String {
    if matches!(style, AccessLog::Json | AccessLog::Logger) {
        return json_line(entry, at);
    }
    let target = target_of(entry);
    let reason = reason_of(entry.status);
    let took = duration_text(entry.elapsed);
    let request = format!(
        "\"{} {} HTTP/{}\"",
        entry.method, target, entry.http_version
    );
    match style {
        AccessLog::Plain | AccessLog::Json | AccessLog::Logger => format!(
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
/// Which answers the access log keeps. Failures (status 400 and up) are always
/// kept, so a health-check exclusion or a slow-only log never hides an error.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LogFilter {
    /// Paths left out: exact, or a prefix when the entry ends with `*`.
    pub skip: Vec<String>,
    /// When set, successful answers faster than this are left out.
    pub slow: Option<Duration>,
    /// Where `AccessLog::Logger` lines go.
    pub sink: Option<Sink>,
}

/// A bounded queue of rendered log lines. A full queue drops the new line: a
/// slow logger never holds a request back or grows memory without bound.
#[derive(Debug, Clone)]
pub struct Sink(SyncSender<String>);

impl Sink {
    pub fn new(capacity: usize) -> (Self, Receiver<String>) {
        let (sender, receiver) = sync_channel(capacity);
        (Self(sender), receiver)
    }

    pub fn push(&self, line: String) {
        let _ = self.0.try_send(line);
    }
}

impl PartialEq for Sink {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}

impl Eq for Sink {}

impl LogFilter {
    pub fn keeps(&self, path: &str, status: u16, elapsed: Duration) -> bool {
        if status >= 400 {
            return true;
        }
        let skipped = self.skip.iter().any(|rule| match rule.strip_suffix('*') {
            Some(prefix) => path.starts_with(prefix),
            None => path == rule,
        });
        !skipped && self.slow.is_none_or(|slow| elapsed >= slow)
    }
}

pub fn log(style: AccessLog, entry: &Entry<'_>, sink: Option<&Sink>) {
    let mut text = line(style, entry);
    if style == AccessLog::Logger {
        if let Some(sink) = sink {
            sink.push(text);
        }
        return;
    }
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
            lane: None,
        }
    }

    #[test]
    fn a_full_sink_drops_instead_of_blocking() {
        let (sink, receiver) = Sink::new(1);
        sink.push("a".into());
        sink.push("b".into());
        assert_eq!(receiver.try_recv().unwrap(), "a");
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn filter_skips_paths_and_slow_floor_but_never_failures() {
        let filter = LogFilter {
            skip: vec!["/health".into(), "/static/*".into()],
            slow: Some(Duration::from_millis(50)),
            sink: None,
        };
        let fast = Duration::from_millis(1);
        let slow = Duration::from_millis(80);
        assert!(!filter.keeps("/health", 200, slow));
        assert!(!filter.keeps("/static/app.js", 200, slow));
        assert!(!filter.keeps("/items", 200, fast));
        assert!(filter.keeps("/items", 200, slow));
        assert!(filter.keeps("/health", 503, fast));
        assert!(LogFilter::default().keeps("/health", 200, fast));
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
    fn json_line_is_one_object_with_a_utc_timestamp() {
        let at = UNIX_EPOCH + Duration::from_millis(1_795_118_185_881);
        let text = line_at(AccessLog::Json, &entry(404, b"a=1", "/items"), at);
        assert_eq!(
            text,
            "{\"time\":\"2026-11-19T19:56:25.881Z\",\"peer\":\"127.0.0.1:52341\",\
             \"method\":\"GET\",\"path\":\"/items\",\"query\":\"a=1\",\
             \"http_version\":\"1.1\",\"status\":404,\"duration_ms\":3.200}"
        );
    }

    #[test]
    fn json_line_names_the_lane_when_known() {
        let mut served = entry(200, b"", "/items");
        served.lane = Some(Lane::Native);
        let text = line_at(AccessLog::Json, &served, UNIX_EPOCH);
        assert!(text.ends_with(",\"lane\":\"native\"}"));
        let trace = Trace::default();
        assert_eq!(trace.lane(), None);
        trace.set(Lane::Python);
        assert_eq!(trace.lane(), Some(Lane::Python));
    }

    #[test]
    fn json_line_escapes_what_a_client_controls() {
        let text = line_at(
            AccessLog::Json,
            &entry(200, b"q=\"x\"\n", "/a\\b\u{1}"),
            UNIX_EPOCH,
        );
        assert!(!text.contains('\n'));
        assert!(text.contains("\"path\":\"/a\\\\b\\u0001\""));
        assert!(text.contains("\"query\":\"q=\\\"x\\\"\\u000a\""));
        assert!(text.contains("\"time\":\"1970-01-01T00:00:00.000Z\""));
    }

    #[test]
    fn dates_across_a_leap_day_are_exact() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
        assert_eq!(civil_from_days(19_782), (2024, 2, 29));
        assert_eq!(civil_from_days(19_783), (2024, 3, 1));
    }

    #[test]
    fn durations_use_a_readable_unit() {
        assert_eq!(duration_text(Duration::from_micros(812)), "812µs");
        assert_eq!(duration_text(Duration::from_micros(3_200)), "3.2ms");
        assert_eq!(duration_text(Duration::from_millis(1_500)), "1.50s");
    }
}
