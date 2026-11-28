//! Response compression: which answers are encoded, and how the head changes.
//!
//! One decision per answer, taken in [`Offer::apply`] after the backend has
//! decided it, so every lane (early, native, Python, WSGI, streams, files) is
//! treated alike. In order, the first rule that applies wins:
//!
//! 1. a status without a body (1xx, 204, 304), `206` or a `content-range`,
//!    a `content-encoding` already set, or `cache-control: no-transform`
//!    leave the answer as it is;
//! 2. a `content-type` that is missing or not allowed (and `text/event-stream`
//!    unless event streams are enabled) leaves it as it is, without `vary`;
//! 3. a file when files are disabled, or a known size below the minimum (or
//!    empty), leaves it as it is, without `vary`;
//! 4. from here the answer can vary with the client: `vary: accept-encoding` is
//!    added (unless a `vary` already names it or is `*`);
//! 5. no coding the client accepts (or no `accept-encoding`) sends it as it is;
//! 6. no free compression slot sends it as it is (no waiting, see
//!    [`Compression::busy`]);
//! 7. otherwise the body is encoded and `content-encoding` is set.
//!
//! A whole body of at most [`EAGER_LIMIT`] bytes is encoded at once and sent
//! with its new `content-length` (or as it was when the result is not shorter);
//! anything longer, and every stream and file, is encoded piece by piece and
//! sent without a `content-length`. A HEAD answer carries the headers its GET
//! would, minus `content-length`, and encodes nothing. It takes no slot, so it
//! does not depend on the load; the GET may still go out as it is (a body of
//! at most [`EAGER_LIMIT`] bytes that does not get shorter, or no free slot).
//! A strong `etag` becomes weak, since the representation changes.

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll, Waker};

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::Body;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::conn::declared_length;
use crate::encode::{encode_all, Coding, Encoded, Levels, Source, MAX_BROTLI_QUALITY};
use crate::headers::Header;
use crate::stream::{Delivery, Reply};

/// The largest whole body encoded at once on the runtime thread.
pub const EAGER_LIMIT: usize = 64 * 1024;
/// Entries of `accept-encoding` that are read; the rest are ignored.
const MAX_ENTRIES: usize = 32;

/// What to compress and how. Hinoki's `CompressionSettings` mirror it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// The codings the server offers, in its order of preference.
    pub codings: Vec<Coding>,
    /// Bodies known to be smaller than this are not compressed.
    pub min_size: u64,
    /// Compressible media types: `type/subtype`, `type/*` or `*/*+suffix`.
    pub types: Vec<String>,
    pub event_streams: bool,
    pub files: bool,
    pub levels: Levels,
    /// Compressions in progress at once; beyond it answers go out as they are.
    pub max_concurrent: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            codings: vec![Coding::Brotli, Coding::Gzip],
            min_size: 1024,
            types: DEFAULT_TYPES
                .iter()
                .map(|rule| (*rule).to_owned())
                .collect(),
            event_streams: false,
            files: true,
            levels: Levels {
                gzip: 5,
                brotli_quality: 4,
                brotli_window: 18,
            },
            max_concurrent: 64,
        }
    }
}

/// The media types compressed unless the settings say otherwise. Mirrors
/// `CompressionSettings.types`; a Python test pins the two together.
pub const DEFAULT_TYPES: [&str; 8] = [
    "text/*",
    "application/json",
    "*/*+json",
    "application/xml",
    "*/*+xml",
    "application/javascript",
    "application/x-ndjson",
    "image/svg+xml",
];

enum MediaRule {
    Exact(String),
    Prefix(String),
    Suffix(String),
}

impl MediaRule {
    fn parse(text: &str) -> Result<Self, String> {
        let rule = text.trim().to_ascii_lowercase();
        let invalid = || format!("{text:?} is not a media type, `type/*` or `*/*+suffix`");
        let (kind, subtype) = rule.split_once('/').ok_or_else(invalid)?;
        let plain = |part: &str| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"!#$&^_.+-".contains(&b))
        };
        match (kind, subtype) {
            ("*", suffix) if suffix.starts_with("*+") && plain(&suffix[2..]) => {
                Ok(Self::Suffix(suffix[1..].to_owned()))
            }
            (kind, "*") if plain(kind) => Ok(Self::Prefix(format!("{kind}/"))),
            (kind, subtype) if plain(kind) && plain(subtype) => Ok(Self::Exact(rule)),
            _ => Err(invalid()),
        }
    }

    fn matches(&self, media_type: &str) -> bool {
        match self {
            Self::Exact(exact) => media_type == exact,
            Self::Prefix(prefix) => media_type.starts_with(prefix.as_str()),
            Self::Suffix(suffix) => media_type.ends_with(suffix.as_str()),
        }
    }
}

/// The compression policy a server applies to its answers.
pub struct Compression {
    config: Config,
    types: Vec<MediaRule>,
    slots: Arc<Semaphore>,
    busy: AtomicU64,
}

impl fmt::Debug for Compression {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Compression")
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

/// Two policies are equal when they are the same one: each owns its slots.
impl PartialEq for Compression {
    fn eq(&self, other: &Self) -> bool {
        std::ptr::eq(self, other)
    }
}

impl Eq for Compression {}

/// What an answer's body is, before compression looks at it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Shape {
    Whole(u64),
    /// A body still being produced, with the `content-length` it declared.
    Live(Option<u64>),
    File(u64),
}

impl Shape {
    pub fn of(delivery: &Delivery) -> Self {
        match delivery {
            // A backend answering HEAD sends no body but declares its length.
            Delivery::Whole(answer) => Self::Whole(match answer.body.len() as u64 {
                0 => declared_length(&answer.headers).ok().flatten().unwrap_or(0),
                size => size,
            }),
            Delivery::Fast(answer) => Self::Whole(match answer.body.len() as u64 {
                0 => answer
                    .headers
                    .get(hyper::header::CONTENT_LENGTH)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| s.parse::<u64>().ok())
                    .unwrap_or(0),
                size => size,
            }),
            Delivery::Streamed(streamed) => {
                Self::Live(declared_length(&streamed.headers).ok().flatten())
            }
            Delivery::File(answer) => Self::File(answer.file.len()),
            Delivery::Upgrade(_) => Self::Whole(0),
        }
    }

    fn size(self) -> Option<u64> {
        match self {
            Self::Whole(size) | Self::File(size) => Some(size),
            Self::Live(declared) => declared,
        }
    }
}

impl Compression {
    /// A policy for `config`, or why it is not usable.
    pub fn new(config: Config) -> Result<Self, String> {
        let bad = |what: &str| Err(format!("compression: {what}"));
        if config.codings.is_empty() {
            return bad("at least one coding is needed");
        }
        if !(1..=9).contains(&config.levels.gzip) {
            return bad("the gzip level is 1 to 9");
        }
        if config.levels.brotli_quality > MAX_BROTLI_QUALITY
            || !(10..=24).contains(&config.levels.brotli_window)
        {
            return bad("the brotli quality is 0 to 9 and its window 10 to 24");
        }
        if config.max_concurrent == 0 {
            return bad("at least one concurrent compression is needed");
        }
        let types = config
            .types
            .iter()
            .map(|text| MediaRule::parse(text))
            .collect::<Result<_, _>>()?;
        Ok(Self {
            slots: Arc::new(Semaphore::new(config.max_concurrent)),
            config,
            types,
            busy: AtomicU64::new(0),
        })
    }

    /// Answers sent as they were because every compression slot was taken.
    pub fn busy(&self) -> u64 {
        self.busy.load(Ordering::Relaxed)
    }

    /// The policy for one request, with the `accept-encoding` it sent.
    pub fn offer(self: &Arc<Self>, headers: &[Header]) -> Offer {
        let mut accept = Vec::new();
        for (_, value) in headers
            .iter()
            .filter(|(name, _)| name.eq_ignore_ascii_case(b"accept-encoding"))
        {
            if !accept.is_empty() {
                accept.push(b',');
            }
            accept.extend_from_slice(value);
        }
        Offer {
            policy: Arc::clone(self),
            accept,
        }
    }

    /// Rules 1 to 3: whether the answer may be compressed at all.
    fn eligible(&self, status: u16, headers: &[Header], shape: Shape) -> bool {
        !has_no_body(status, headers)
            && !headers_named(headers, "content-encoding").any(|_| true)
            && !no_transform(headers)
            && self.compressible(headers)
            && self.worth_it(shape)
    }

    fn compressible(&self, headers: &[Header]) -> bool {
        let Some(media_type) = media_type(headers) else {
            return false;
        };
        if media_type == "text/event-stream" {
            return self.config.event_streams;
        }
        self.types.iter().any(|rule| rule.matches(&media_type))
    }

    fn worth_it(&self, shape: Shape) -> bool {
        if matches!(shape, Shape::File(_)) && !self.config.files {
            return false;
        }
        shape
            .size()
            .is_none_or(|size| size > 0 && size >= self.config.min_size)
    }

    fn slot(&self) -> Option<OwnedSemaphorePermit> {
        let slot = Arc::clone(&self.slots).try_acquire_owned().ok();
        if slot.is_none() {
            self.busy.fetch_add(1, Ordering::Relaxed);
        }
        slot
    }

    fn whole(
        &self,
        coding: Coding,
        headers: &mut Vec<Header>,
        full: Full<Bytes>,
        slot: OwnedSemaphorePermit,
    ) -> Reply {
        let bytes = whole_bytes(full);
        if bytes.len() > EAGER_LIMIT {
            let source = Source::Memory(bytes);
            return self.encoded(coding, headers, source, slot);
        }
        match encode_all(coding, self.config.levels, &bytes).filter(|out| out.len() < bytes.len()) {
            Some(out) => {
                announce(headers, coding);
                set_length(headers, out.len());
                Reply::Whole(Full::new(out))
            }
            None => Reply::Whole(Full::new(bytes)),
        }
    }

    fn encoded(
        &self,
        coding: Coding,
        headers: &mut Vec<Header>,
        source: Source,
        slot: OwnedSemaphorePermit,
    ) -> Reply {
        announce(headers, coding);
        headers.retain(|(name, _)| !name.eq_ignore_ascii_case(b"content-length"));
        Reply::Encoded(Box::new(Encoded::new(
            source,
            coding,
            self.config.levels,
            slot,
        )))
    }
}

/// The compression policy as one request sees it.
pub struct Offer {
    policy: Arc<Compression>,
    accept: Vec<u8>,
}

impl Offer {
    /// Apply the policy to an answer's head and body.
    pub fn apply(
        &self,
        status: u16,
        headers: &mut Vec<Header>,
        body: Reply,
        shape: Shape,
        head_only: bool,
    ) -> Reply {
        let policy = &self.policy;
        if !policy.eligible(status, headers, shape) {
            return body;
        }
        add_vary(headers);
        let Some(coding) = negotiate(&self.accept, &policy.config.codings) else {
            return body;
        };
        if head_only {
            announce(headers, coding);
            headers.retain(|(name, _)| !name.eq_ignore_ascii_case(b"content-length"));
            return body;
        }
        let Some(slot) = policy.slot() else {
            return body;
        };
        match body {
            Reply::Whole(full) => policy.whole(coding, headers, full, slot),
            Reply::Live(live) => policy.encoded(coding, headers, Source::Live(live), slot),
            Reply::File(file) => policy.encoded(coding, headers, Source::File(file), slot),
            already @ (Reply::Encoded(_) | Reply::Counted(_)) => already,
        }
    }
}

/// The bytes of a body that is already in memory.
pub(crate) fn whole_bytes(mut full: Full<Bytes>) -> Bytes {
    // A `Full` is always ready; the waker is never used.
    let mut cx = Context::from_waker(Waker::noop());
    match std::pin::Pin::new(&mut full).poll_frame(&mut cx) {
        Poll::Ready(Some(Ok(frame))) => frame.into_data().unwrap_or_default(),
        _ => Bytes::new(),
    }
}

fn headers_named<'a>(headers: &'a [Header], name: &'a str) -> impl Iterator<Item = &'a [u8]> {
    headers
        .iter()
        .filter(move |(candidate, _)| candidate.eq_ignore_ascii_case(name.as_bytes()))
        .map(|(_, value)| value.as_slice())
}

fn has_no_body(status: u16, headers: &[Header]) -> bool {
    matches!(status, 100..=199 | 204 | 206 | 304)
        || headers_named(headers, "content-range").any(|_| true)
}

/// The comma separated tokens of a header value, lowercase.
fn tokens(value: &[u8]) -> impl Iterator<Item = String> + '_ {
    value
        .split(|&b| b == b',')
        .map(|token| String::from_utf8_lossy(token.trim_ascii()).to_ascii_lowercase())
}

fn no_transform(headers: &[Header]) -> bool {
    headers_named(headers, "cache-control")
        .flat_map(tokens)
        .any(|directive| directive == "no-transform")
}

/// The media type of the `content-type`, without parameters, lowercase.
fn media_type(headers: &[Header]) -> Option<String> {
    let value = headers_named(headers, "content-type").next()?;
    let text = String::from_utf8_lossy(value);
    let essence = text.split(';').next().unwrap_or("").trim();
    (!essence.is_empty()).then(|| essence.to_ascii_lowercase())
}

fn add_vary(headers: &mut Vec<Header>) {
    let covered = headers_named(headers, "vary")
        .flat_map(tokens)
        .any(|token| token == "*" || token == "accept-encoding");
    if !covered {
        headers.push((b"vary".to_vec(), b"accept-encoding".to_vec()));
    }
}

/// Set `content-encoding` and weaken a strong `etag`.
fn announce(headers: &mut Vec<Header>, coding: Coding) {
    for (name, value) in headers.iter_mut() {
        if name.eq_ignore_ascii_case(b"etag") && !value.starts_with(b"W/") {
            let mut weak = b"W/".to_vec();
            weak.extend_from_slice(value);
            *value = weak;
        }
    }
    headers.push((
        b"content-encoding".to_vec(),
        coding.token().as_bytes().to_vec(),
    ));
}

fn set_length(headers: &mut [Header], length: usize) {
    for (name, value) in headers.iter_mut() {
        if name.eq_ignore_ascii_case(b"content-length") {
            *value = length.to_string().into_bytes();
        }
    }
}

/// `q` of an `accept-encoding` entry in thousandths; `None` when malformed.
fn quality(text: &str) -> Option<u16> {
    let (whole, fraction) = text.split_once('.').unwrap_or((text, ""));
    let whole: u16 = match whole {
        "0" => 0,
        "1" => 1,
        _ => return None,
    };
    if fraction.len() > 3 || !fraction.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let fraction: u16 = format!("{fraction:0<3}").parse().ok()?;
    let q = whole * 1000 + fraction;
    (q <= 1000).then_some(q)
}

/// One `accept-encoding` entry: its lowercase coding and `q` in thousandths.
fn entry(text: &[u8]) -> Option<(String, u16)> {
    let text = String::from_utf8_lossy(text);
    let mut parts = text.split(';');
    let name = parts.next()?.trim().to_ascii_lowercase();
    if name.is_empty() {
        return None;
    }
    let mut q = 1000;
    for parameter in parts {
        let (key, value) = parameter.split_once('=').unwrap_or((parameter, ""));
        if key.trim().eq_ignore_ascii_case("q") {
            q = quality(value.trim())?;
        }
    }
    Some((name, q))
}

/// The coding to use for an `accept-encoding` (RFC 9110 12.5.3): the highest
/// `q` the client gives among `codings`, the server's order breaking ties. A
/// coding the client does not name takes the `q` of `*`; with no header
/// nothing is accepted. Malformed entries are ignored.
pub fn negotiate(accept: &[u8], codings: &[Coding]) -> Option<Coding> {
    let entries: Vec<(String, u16)> = accept
        .split(|&b| b == b',')
        .take(MAX_ENTRIES)
        .filter_map(entry)
        .collect();
    let star = entries.iter().find(|(name, _)| name == "*").map(|e| e.1);
    let quality_of = |coding: Coding| {
        let names: &[&str] = match coding {
            Coding::Brotli => &["br"],
            Coding::Gzip => &["gzip", "x-gzip"],
        };
        let named = entries
            .iter()
            .find(|(name, _)| names.contains(&name.as_str()));
        named.map(|e| e.1).or(star).unwrap_or(0)
    };
    let mut best: Option<(u16, Coding)> = None;
    for &coding in codings {
        let q = quality_of(coding);
        if q > 0 && best.is_none_or(|(top, _)| q > top) {
            best = Some((q, coding));
        }
    }
    best.map(|(_, coding)| coding)
}
