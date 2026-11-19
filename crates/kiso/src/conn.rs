//! One HTTP/1.1 or HTTP/2 request on one connection: what the transport hands to the
//! [`Backend`] and how the [`Answer`] is written back.
//!
//! This layer owns only transport rules: HEAD answers keep their
//! `content-length` and send no body, a request body is handed to the backend
//! unread (as [`Incoming::body`]) and read only when the backend asks, and a
//! request the protocol forbids (HTTP/1.1 without exactly one `Host`) is flagged
//! in [`Incoming::refusal`], so the backend answers it like any other early
//! answer; refused body framing never gets here (see [`crate::framing`]). The
//! connection closes after
//! any answer given while a body was not read in full, so the leftover bytes are
//! never parsed as the next request. Everything that decides an answer belongs
//! to the backend.

use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Incoming as RequestBody};
use hyper::header::{HeaderName, HeaderValue};
use hyper::http::request::Parts;
use hyper::{Method, Request, Response, StatusCode, Version};

use crate::accesslog::{self, AccessLog, LogFilter, Sink, Trace};
use crate::body::BodyReader;
use crate::early::Early;
use crate::framing::Signals;
use crate::headers::Header;

/// The head of a request as the backend sees it. Header names are lowercase,
/// repeated headers stay separate and in arrival order, and `path` and `query`
/// are the raw (still percent-encoded) target. An absolute-form target makes its
/// authority the `host` header, and so does the `:authority` of an HTTP/2
/// request, which wins over a `host` header that differs (RFC 9113 8.3.1).
#[derive(Debug)]
pub struct Incoming {
    pub method: String,
    pub path: String,
    pub query: Vec<u8>,
    pub headers: Vec<Header>,
    pub peer: SocketAddr,
    /// The connection is TLS: the request arrived over https.
    pub secure: bool,
    /// `"2"` for a request that arrived over HTTP/2, `"1.1"` otherwise.
    pub http_version: &'static str,
    /// Set when the transport rejects the request ([`Early::BadHost`]): the
    /// backend answers with it and does not route the request.
    pub refusal: Option<Early>,
    /// The request body, unread; `None` when the request has none. Nothing is
    /// polled (and no `100 Continue` sent) until the backend reads it.
    pub body: Option<BodyReader>,
    /// Filled in by the backend with where it served the request, for the log.
    pub trace: Arc<Trace>,
}

/// A complete answer: final status, final headers and the whole body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Answer {
    pub status: u16,
    pub headers: Vec<Header>,
    pub body: Vec<u8>,
}

/// Decides every answer. It runs on a tokio thread and must not need the GIL.
pub trait Backend: Send + Sync + 'static {
    fn handle(&self, request: Incoming) -> impl Future<Output = Answer> + Send;
}

fn plain(status: StatusCode) -> Response<Full<Bytes>> {
    let mut response = Response::new(Full::new(Bytes::new()));
    *response.status_mut() = status;
    response
}

/// The transport's verdict on a request: HTTP/1.1 needs exactly one `Host`
/// (RFC 9112 3.2). An absolute-form target (RFC 9112 3.2.2) makes its authority
/// the host, replacing the header. HTTP/2 has no Host rule, but without
/// `:authority` more than one `host` header is as ambiguous as on HTTP/1.1 and
/// is refused the same way.
fn refusal_of(parts: &Parts, headers: &mut Vec<Header>) -> Option<Early> {
    let hosts = headers.iter().filter(|(name, _)| name == b"host").count();
    if let Some(authority) = parts.uri.authority() {
        headers.retain(|(name, _)| name != b"host");
        headers.push((b"host".to_vec(), authority.as_str().as_bytes().to_vec()));
    }
    let ambiguous = match parts.version {
        Version::HTTP_11 => hosts != 1,
        Version::HTTP_2 => parts.uri.authority().is_none() && hosts > 1,
        _ => false,
    };
    ambiguous.then_some(Early::BadHost)
}

fn incoming_of(
    parts: &Parts,
    body: Option<BodyReader>,
    peer: SocketAddr,
    transport: &Transport,
) -> Incoming {
    let mut headers: Vec<Header> = parts
        .headers
        .iter()
        .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
        .collect();
    // hyper enforces `max_headers` on HTTP/1 while parsing; h2 only bounds the
    // list by size, so the count is checked here.
    // h2 sends one `cookie` field per crumb (RFC 9113 8.2.3); they are one line
    // on HTTP/1, so they count once.
    let crumbs = headers.iter().filter(|(name, _)| name == b"cookie").count();
    let fields = headers.len() - crumbs.saturating_sub(1);
    let refusal = if fields > transport.max_headers {
        Some(Early::HeadersTooLarge)
    } else {
        refusal_of(parts, &mut headers)
    };
    Incoming {
        method: parts.method.as_str().to_owned(),
        path: parts.uri.path().to_owned(),
        query: parts.uri.query().unwrap_or("").as_bytes().to_vec(),
        headers,
        peer,
        secure: transport.secure,
        http_version: if parts.version == Version::HTTP_2 {
            "2"
        } else {
            "1.1"
        },
        refusal,
        body,
        trace: Arc::default(),
    }
}

/// Build the hyper response. An answer the transport cannot represent (a status
/// or header hyper rejects) becomes a bare 500 rather than a broken stream. So
/// does an informational status: `101` would hand the connection to hyper's
/// upgrade path (and h2 has no 101), and the server serves no protocol that
/// switches.
fn response_of(answer: Answer, head_only: bool) -> Response<Full<Bytes>> {
    let Ok(status) = StatusCode::from_u16(answer.status) else {
        return plain(StatusCode::INTERNAL_SERVER_ERROR);
    };
    if status.is_informational() {
        return plain(StatusCode::INTERNAL_SERVER_ERROR);
    }
    let body = if head_only {
        Bytes::new()
    } else {
        Bytes::from(answer.body)
    };
    let mut response = Response::new(Full::new(body));
    *response.status_mut() = status;
    for (name, value) in &answer.headers {
        let Ok(name) = HeaderName::from_bytes(name) else {
            return plain(StatusCode::INTERNAL_SERVER_ERROR);
        };
        let Ok(value) = HeaderValue::from_bytes(value) else {
            return plain(StatusCode::INTERNAL_SERVER_ERROR);
        };
        response.headers_mut().append(name, value);
    }
    response
}

/// What the connection layer shares with every request it serves.
#[derive(Debug, Clone)]
pub struct Transport {
    /// How long a client may stay silent while its body is read.
    pub body_read_timeout: Duration,
    /// Whether the connection is TLS.
    pub secure: bool,
    /// Most headers a request may carry; more is answered 431.
    pub max_headers: usize,
    /// What the framing guard and this layer tell each other about the connection.
    pub signals: Arc<Signals>,
    /// Log every answer in this style; `None` logs nothing.
    pub access_log: Option<AccessLog>,
    /// Which answers that log keeps.
    pub log_filter: Arc<LogFilter>,
}

/// The parts of a request an access-log line needs, kept past the handoff.
struct LoggedHead {
    peer: SocketAddr,
    method: String,
    path: String,
    query: Vec<u8>,
    http_version: &'static str,
    trace: Arc<Trace>,
}

impl LoggedHead {
    fn log(&self, style: AccessLog, status: u16, elapsed: Duration, sink: Option<&Sink>) {
        accesslog::log(
            style,
            &accesslog::Entry {
                peer: self.peer,
                method: &self.method,
                path: &self.path,
                query: &self.query,
                http_version: self.http_version,
                status,
                elapsed,
                lane: self.trace.lane(),
                route: self.trace.route(),
            },
            sink,
        );
    }
}

impl Incoming {
    fn clone_head(&self) -> LoggedHead {
        LoggedHead {
            peer: self.peer,
            method: self.method.clone(),
            path: self.path.clone(),
            query: self.query.clone(),
            http_version: self.http_version,
            trace: Arc::clone(&self.trace),
        }
    }
}

/// Serve one request: ask the backend, write the answer. The connection closes
/// after an answer given while the body was not read in full (unread, refused,
/// cut short) and when the framing guard asks for it: in each case the bytes that follow must not be parsed as a request.
pub async fn respond<B: Backend>(
    backend: Arc<B>,
    peer: SocketAddr,
    transport: Transport,
    request: Request<RequestBody>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let head_only = request.method() == Method::HEAD;
    let version = request.version();
    let (parts, body) = request.into_parts();
    let body = (!body.is_end_stream()).then(|| BodyReader::new(body, transport.body_read_timeout));
    let finished = body.as_ref().map(BodyReader::finished);
    let incoming = incoming_of(&parts, body, peer, &transport);
    let started = Instant::now();
    let logged = transport
        .access_log
        .map(|style| (style, incoming.clone_head()));
    let answer = backend.handle(incoming).await;
    if let Some((style, head)) = logged {
        let elapsed = started.elapsed();
        if transport
            .log_filter
            .keeps(&head.path, answer.status, elapsed)
        {
            head.log(
                style,
                answer.status,
                elapsed,
                transport.log_filter.sink.as_ref(),
            );
        }
    }
    let mut response = response_of(answer, head_only);
    let unread = finished.is_some_and(|done| !done.load(Ordering::Acquire));
    if unread {
        transport.signals.linger.store(true, Ordering::Release);
    }
    // `connection` is a forbidden header on HTTP/2, which has no such notion.
    let closing = unread || transport.signals.close.load(Ordering::Acquire);
    if closing && version != Version::HTTP_2 {
        response
            .headers_mut()
            .insert("connection", HeaderValue::from_static("close"));
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn answer(status: u16, headers: Vec<Header>, body: &[u8]) -> Answer {
        Answer {
            status,
            headers,
            body: body.to_vec(),
        }
    }

    #[test]
    fn head_keeps_the_declared_length_and_drops_the_body() {
        let headers = vec![(b"content-length".to_vec(), b"5".to_vec())];
        let response = response_of(answer(200, headers, b"hello"), true);
        assert_eq!(response.headers()["content-length"], "5");
        assert!(response.body().is_end_stream());
    }

    #[test]
    fn repeated_headers_stay_separate() {
        let headers = vec![
            (b"set-cookie".to_vec(), b"a=1".to_vec()),
            (b"set-cookie".to_vec(), b"b=2".to_vec()),
        ];
        let response = response_of(answer(200, headers, b""), false);
        assert_eq!(response.headers().get_all("set-cookie").iter().count(), 2);
    }

    #[test]
    fn unrepresentable_answers_become_a_bare_500() {
        let bad_status = response_of(answer(99, vec![], b""), false);
        assert_eq!(bad_status.status(), 500);
        assert_eq!(response_of(answer(101, vec![], b""), false).status(), 500);
        let bad_name = vec![(b"bad name".to_vec(), b"x".to_vec())];
        assert_eq!(response_of(answer(200, bad_name, b""), false).status(), 500);
        let bad_value = vec![(b"x".to_vec(), b"a\nb".to_vec())];
        assert_eq!(
            response_of(answer(200, bad_value, b""), false).status(),
            500
        );
    }
}
