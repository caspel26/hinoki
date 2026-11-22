//! Request head framing guard.
//!
//! httparse (hence hyper) accepts a bare LF as a line terminator, so
//! `x: a\ncontent-length: 5` parses as two headers while a proxy that splits only
//! on CRLF sees one: a request-smuggling shape. hyper has no option to refuse
//! it, so [`Guarded`] wraps the socket and runs every byte it reads through
//! [`Framing`], which follows the message boundaries of the connection:
//!
//! * in a head, a LF not preceded by CR is rewritten to NUL, which httparse
//!   rejects: hyper answers its own bare 400 and closes the connection. The
//!   same is done to the line that completes a conflicting framing:
//!   `Content-Length` together with `Transfer-Encoding` (hyper would let the
//!   latter win and hide the former), a repeated `Content-Length`, one that is
//!   not a single run of digits, a repeated `Transfer-Encoding` or a coding
//!   other than exactly `chunked`;
//! * a body with a plain `Content-Length` is skipped by count, never scanned
//!   (a JSON body may hold any byte);
//! * a head above the bound is refused the same way (hyper's own
//!   `max_header_size` is set to the same bound);
//! * a chunked body stops the scan and sets the `close` flag, so the connection layer ends the connection after that
//!   answer and no later head goes unchecked. A chunked request therefore does
//!   not keep the connection alive.
//!
//! The refusals are hyper's bare 400 (no JSON body, no request id), like its
//! other head backstops.

use std::future::Future;
use std::io;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::task::{ready, Context, Poll};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::time::{sleep, Sleep};

use crate::stall::WriteStall;

/// How long, and how much, a closing connection reads after the answer when
/// part of the request was left unread. Closing a socket that still holds
/// unread input resets it (RST), and the client may then lose the answer it has
/// not read yet; reading until the client stops (or these bounds) avoids that.
const LINGER: Duration = Duration::from_secs(2);
const LINGER_BYTES: usize = 1024 * 1024;

/// What the connection layer and the guard tell each other about one connection.
#[derive(Debug, Default)]
pub struct Signals {
    /// The connection must end after the current answer.
    pub close: AtomicBool,
    /// Request bytes were left unread: drain before the socket is dropped.
    pub linger: AtomicBool,
    /// The connection is a WebSocket now: its bytes are not HTTP and are not scanned.
    pub upgraded: AtomicBool,
}

const HEAD_END: &[u8] = b"\r\n\r\n";

#[derive(Debug, PartialEq, Eq)]
enum Phase {
    /// Collecting a head; `collected` holds it so far.
    Head,
    /// Skipping this many body bytes.
    Body(u64),
    /// Not scanned any more.
    Opaque,
}

/// A header line, as far as the framing is concerned.
enum Line {
    Length(Option<u64>),
    /// `Transfer-Encoding`; `true` when its value is exactly `chunked`.
    Coding(bool),
    Other,
}

fn classify(line: &[u8]) -> Line {
    let Some(colon) = line.iter().position(|&byte| byte == b':') else {
        return Line::Other;
    };
    let (name, value) = (&line[..colon], &line[colon + 1..]);
    if name.eq_ignore_ascii_case(b"transfer-encoding") {
        Line::Coding(blanks_trimmed(value).eq_ignore_ascii_case(b"chunked"))
    } else if name.eq_ignore_ascii_case(b"content-length") {
        Line::Length(plain_length(blanks_trimmed(value)))
    } else {
        Line::Other
    }
}

fn blanks_trimmed(value: &[u8]) -> &[u8] {
    let blank = |byte: &u8| matches!(byte, b' ' | b'\t' | b'\r' | b'\n');
    let start = value.iter().position(|b| !blank(b)).unwrap_or(value.len());
    let end = value
        .iter()
        .rposition(|b| !blank(b))
        .map_or(start, |i| i + 1);
    &value[start..end]
}

/// A single run of digits that fits a `u64`.
fn plain_length(digits: &[u8]) -> Option<u64> {
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(digits).ok()?.parse().ok()
}

/// The scanner. Feed it every byte read from the client, in order.
#[derive(Debug)]
pub struct Framing {
    phase: Phase,
    collected: Vec<u8>,
    line_start: usize,
    /// What the head being collected has declared so far.
    lengths: u8,
    declared: Option<u64>,
    chunked: bool,
    previous: u8,
    max_head: usize,
    signals: Arc<Signals>,
}

impl Framing {
    /// `max_head` bounds what is collected for one head; `signals.close` is set
    /// when the scan gives up.
    pub fn new(max_head: usize, signals: Arc<Signals>) -> Self {
        Self {
            phase: Phase::Head,
            collected: Vec::new(),
            line_start: 0,
            lengths: 0,
            declared: None,
            chunked: false,
            previous: 0,
            max_head,
            signals,
        }
    }

    pub fn feed(&mut self, bytes: &mut [u8]) {
        let mut at = 0;
        while at < bytes.len() && self.phase != Phase::Opaque {
            at += match self.phase {
                Phase::Body(left) => self.skip(left, bytes.len() - at),
                _ => self.scan_head(&mut bytes[at..]),
            };
        }
    }

    fn skip(&mut self, left: u64, available: usize) -> usize {
        let taken = usize::try_from(left).map_or(available, |n| n.min(available));
        let remaining = left - taken as u64;
        self.phase = if remaining == 0 {
            Phase::Head
        } else {
            Phase::Body(remaining)
        };
        taken
    }

    /// Scan until the head ends or the bytes run out; returns how many bytes it used.
    fn scan_head(&mut self, bytes: &mut [u8]) -> usize {
        for (index, byte) in bytes.iter_mut().enumerate() {
            let bare = *byte == b'\n' && self.previous != b'\r';
            self.previous = *byte;
            self.collected.push(*byte);
            // A head above the bound fails closed like any other refusal: the
            // NUL makes httparse refuse it, so nothing past the bound is
            // accepted unscanned.
            let oversized = self.collected.len() > self.max_head;
            if bare || oversized || (*byte == b'\n' && self.line_refused()) {
                *byte = 0;
                self.give_up();
                return index + 1;
            }
            if self.collected.ends_with(HEAD_END) {
                self.end_head();
                return index + 1;
            }
        }
        bytes.len()
    }

    /// Account for the header line just completed; `true` when it conflicts
    /// with what the head declared before.
    fn line_refused(&mut self) -> bool {
        let line = &self.collected[self.line_start..];
        let kind = classify(line);
        self.line_start = self.collected.len();
        match kind {
            Line::Length(declared) => {
                self.lengths = self.lengths.saturating_add(1);
                self.declared = declared;
                self.chunked || self.lengths > 1 || declared.is_none()
            }
            Line::Coding(plain) => {
                let repeated = self.chunked;
                self.chunked = true;
                !plain || repeated || self.lengths > 0
            }
            Line::Other => false,
        }
    }

    fn end_head(&mut self) {
        self.phase = match (self.chunked, self.lengths, self.declared) {
            (false, 0, _) | (false, 1, Some(0)) => Phase::Head,
            (false, 1, Some(n)) => Phase::Body(n),
            _ => Phase::Opaque,
        };
        self.reset_head();
        if self.phase == Phase::Opaque {
            self.signals.close.store(true, Ordering::Release);
        }
    }

    fn reset_head(&mut self) {
        self.collected.clear();
        self.line_start = 0;
        self.lengths = 0;
        self.declared = None;
        self.chunked = false;
        self.previous = 0;
    }

    fn give_up(&mut self) {
        self.phase = Phase::Opaque;
        self.reset_head();
        self.collected = Vec::new();
        self.signals.close.store(true, Ordering::Release);
        // hyper answers its 400 while the client may still be sending.
        self.signals.linger.store(true, Ordering::Release);
    }
}

/// A stream whose reads go through [`Framing`], and which lingers at shutdown
/// when [`Signals::linger`] is set.
pub struct Guarded<T> {
    inner: T,
    framing: Framing,
    signals: Arc<Signals>,
    shut: bool,
    drained: usize,
    deadline: Option<Pin<Box<Sleep>>>,
    stall: WriteStall,
}

impl<T> Guarded<T> {
    /// `write_timeout` is how long a write may make no progress (a client that
    /// stopped reading) before the connection fails with `TimedOut`.
    pub fn new(inner: T, framing: Framing, signals: Arc<Signals>, write_timeout: Duration) -> Self {
        Self {
            inner,
            framing,
            signals,
            shut: false,
            drained: 0,
            deadline: None,
            stall: WriteStall::new(write_timeout),
        }
    }
}

impl<T: AsyncRead + Unpin> Guarded<T> {
    /// Read and discard until the client stops, or the bounds are reached.
    fn linger(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let deadline = self.deadline.get_or_insert_with(|| Box::pin(sleep(LINGER)));
        if deadline.as_mut().poll(cx).is_ready() {
            return Poll::Ready(Ok(()));
        }
        let mut scratch = [0_u8; 8192];
        while self.drained < LINGER_BYTES {
            let mut buf = ReadBuf::new(&mut scratch);
            match Pin::new(&mut self.inner).poll_read(cx, &mut buf) {
                Poll::Ready(Ok(())) if !buf.filled().is_empty() => {
                    self.drained += buf.filled().len();
                }
                Poll::Ready(_) => break,
                Poll::Pending => return Poll::Pending,
            }
        }
        Poll::Ready(Ok(()))
    }
}

impl<T: AsyncRead + Unpin> AsyncRead for Guarded<T> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let before = buf.filled().len();
        let polled = Pin::new(&mut this.inner).poll_read(cx, buf);
        if let Poll::Ready(Ok(())) = polled {
            if !this.signals.upgraded.load(Ordering::Acquire) {
                this.framing.feed(&mut buf.filled_mut()[before..]);
            }
        }
        polled
    }
}

impl<T: AsyncRead + AsyncWrite + Unpin> AsyncWrite for Guarded<T> {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_write(cx, data);
        this.stall.guard(cx, polled)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_flush(cx);
        this.stall.guard(cx, polled)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if !this.shut {
            // Over TLS this writes `close_notify`, which a client that stopped
            // reading can stall: the stall timer covers it too.
            let polled = Pin::new(&mut this.inner).poll_shutdown(cx);
            ready!(this.stall.guard(cx, polled))?;
            this.shut = true;
        }
        if this.signals.linger.load(Ordering::Acquire) {
            return this.linger(cx);
        }
        Poll::Ready(Ok(()))
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[io::IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        let polled = Pin::new(&mut this.inner).poll_write_vectored(cx, bufs);
        this.stall.guard(cx, polled)
    }

    fn is_write_vectored(&self) -> bool {
        self.inner.is_write_vectored()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn framing() -> (Framing, Arc<Signals>) {
        let signals = Arc::new(Signals::default());
        (Framing::new(1024, Arc::clone(&signals)), signals)
    }

    /// Feed `chunks` as separate reads; return the bytes as the guard hands them on.
    fn run(framing: &mut Framing, chunks: &[&[u8]]) -> Vec<u8> {
        let mut out = Vec::new();
        for chunk in chunks {
            let mut bytes = chunk.to_vec();
            framing.feed(&mut bytes);
            out.extend(bytes);
        }
        out
    }

    #[test]
    fn a_crlf_head_passes_untouched() {
        let (mut f, close) = framing();
        let head = b"GET / HTTP/1.1\r\nhost: t\r\n\r\n";
        assert_eq!(run(&mut f, &[head]), head);
        assert!(!close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_bare_lf_in_a_header_becomes_nul() {
        let (mut f, close) = framing();
        let out = run(
            &mut f,
            &[b"GET / HTTP/1.1\r\nx: a\ncontent-length: 5\r\n\r\n"],
        );
        assert_eq!(out, b"GET / HTTP/1.1\r\nx: a\0content-length: 5\r\n\r\n");
        assert!(close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_bare_lf_in_the_request_line_becomes_nul() {
        let (mut f, _) = framing();
        let out = run(&mut f, &[b"GET / HTTP/1.1\nhost: t\r\n\r\n"]);
        assert_eq!(out[14], 0);
    }

    #[test]
    fn a_cr_and_its_lf_may_arrive_in_different_reads() {
        let (mut f, close) = framing();
        let head: &[u8] = b"GET / HTTP/1.1\r\nhost: t\r\n\r\n";
        let out = run(
            &mut f,
            &[b"GET / HTTP/1.1\r", b"\nhost: t\r", b"\n\r", b"\n"],
        );
        assert_eq!(out, head);
        assert!(!close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_content_length_body_is_skipped_by_count() {
        let (mut f, close) = framing();
        let request = b"POST / HTTP/1.1\r\ncontent-length: 7\r\n\r\n{\n  1\n}";
        assert_eq!(run(&mut f, &[request]), request);
        assert!(!close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_head_after_the_body_is_scanned_again() {
        let (mut f, close) = framing();
        let out = run(
            &mut f,
            &[
                b"POST / HTTP/1.1\r\ncontent-length: 2\r\n\r\nab",
                b"GET / HTTP/1.1\nx: y\r\n\r\n",
            ],
        );
        assert_eq!(out.iter().filter(|&&b| b == 0).count(), 1);
        assert!(close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_pipelined_bare_lf_in_the_same_read_is_caught() {
        let (mut f, close) = framing();
        let out = run(
            &mut f,
            &[b"POST / HTTP/1.1\r\ncontent-length: 2\r\n\r\nabGET / HTTP/1.1\nx: y\r\n\r\n"],
        );
        assert_eq!(out.iter().filter(|&&b| b == 0).count(), 1);
        assert!(close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_body_may_arrive_in_many_reads() {
        let (mut f, close) = framing();
        let reads: [&[u8]; 4] = [
            b"POST / HTTP/1.1\r\ncontent-length: 4\r\n\r\n",
            b"a\n",
            b"b\n",
            b"x",
        ];
        let out = run(&mut f, &reads);
        assert_eq!(out, reads.concat());
        // The `x` already belongs to the next head, which is incomplete.
        assert!(!close.close.load(Ordering::Acquire));
    }

    #[test]
    fn a_chunked_body_stops_the_scan_and_asks_to_close() {
        let (mut f, close) = framing();
        let request = b"POST / HTTP/1.1\r\ntransfer-encoding: chunked\r\n\r\n2\nab\r\n";
        assert_eq!(run(&mut f, &[request]), request);
        assert!(close.close.load(Ordering::Acquire));
    }

    #[test]
    fn conflicting_framing_is_refused_at_the_line_that_completes_it() {
        let cases: [(&[u8], &str); 8] = [
            (
                b"content-length: 1
content-length: 1

",
                "repeated length",
            ),
            (
                b"content-length: 1, 1

",
                "length list",
            ),
            (
                b"content-length: +1

",
                "signed length",
            ),
            (
                b"content-length: 99999999999999999999999

",
                "overflow",
            ),
            (
                b"content-length: 2
transfer-encoding: chunked
\r
",
                "length first",
            ),
            (
                b"transfer-encoding: chunked
content-length: 2

",
                "coding first",
            ),
            (
                b"transfer-encoding: gzip, chunked

",
                "other coding",
            ),
            (
                b"transfer-encoding: chunked
transfer-encoding: chunked

",
                "repeated coding",
            ),
        ];
        for (headers, name) in cases {
            let (mut f, close) = framing();
            let head = [&b"POST / HTTP/1.1\r\n"[..], headers].concat();
            let out = run(&mut f, &[&head]);
            assert_eq!(out.iter().filter(|&&b| b == 0).count(), 1, "{name}");
            assert!(close.close.load(Ordering::Acquire), "{name}");
        }
    }

    #[test]
    fn a_zero_length_is_a_head_without_a_body() {
        let (mut f, close) = framing();
        let request = b"POST / HTTP/1.1\r\ncontent-length: 0\r\n\r\nGET / HTTP/1.1\r\n\r\n";
        assert_eq!(run(&mut f, &[request]), request);
        assert!(!close.close.load(Ordering::Acquire));
    }

    #[test]
    fn an_oversized_head_stops_the_scan_and_asks_to_close() {
        let close = Arc::new(Signals::default());
        let mut f = Framing::new(16, Arc::clone(&close));
        run(&mut f, &[b"GET /aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]);
        assert!(close.close.load(Ordering::Acquire));
    }

    #[test]
    fn leading_empty_lines_are_not_a_head() {
        let (mut f, close) = framing();
        let request = b"\r\n\r\nGET / HTTP/1.1\r\nhost: t\r\n\r\n";
        assert_eq!(run(&mut f, &[request]), request);
        assert!(!close.close.load(Ordering::Acquire));
    }
}
