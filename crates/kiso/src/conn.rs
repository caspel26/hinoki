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
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Incoming as RequestBody};
use hyper::header::{HeaderName, HeaderValue};
use hyper::http::request::Parts;
use hyper::upgrade::OnUpgrade;
use hyper::{Method, Request, Response, StatusCode, Version};

use crate::accesslog::{self, AccessLog, Lane, LogFilter, Sink, Trace};
use crate::body::BodyReader;
use crate::compress::{Compression, Offer, Shape};
use crate::early::Early;
use crate::framing::Signals;
use crate::headers::Header;
use crate::metrics::{ErrorKind, InFlightGuard, Registry};
use crate::stream::{self, Counted, Delivery, FileAnswer, Live, Reply, Streamed};
use crate::ws::{self, Upgrade};
use crate::wsrun::{Accepted, Session};

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
    /// Whether the request asks to switch to WebSocket (HTTP/1.1 only).
    pub upgrade: Upgrade,
}

/// A complete answer: final status, final headers and the whole body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Answer {
    pub status: u16,
    pub headers: Vec<Header>,
    pub body: Bytes,
}

/// Decides every answer. It runs on a tokio thread and must not need the GIL.
pub trait Backend: Send + Sync + 'static {
    fn handle(&self, request: Incoming) -> impl Future<Output = Answer> + Send;

    /// Like [`Backend::handle`], for a backend that may answer with a body it
    /// is still producing. The default answers with [`Backend::handle`].
    fn deliver(&self, request: Incoming) -> impl Future<Output = Delivery> + Send {
        async move { Delivery::Whole(self.handle(request).await) }
    }
}

fn plain(status: StatusCode) -> Response<Reply> {
    let mut response = Response::new(Reply::Whole(Full::new(Bytes::new())));
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
        _ => hosts > 1,
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
    let upgrade = if parts.version == Version::HTTP_2 {
        Upgrade::Absent
    } else {
        ws::inspect(parts.method.as_str(), &headers)
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
        upgrade,
    }
}

/// The status, headers and body of a delivery. A HEAD answer sends no body and
/// drops a stream's receiver (or closes a file), which tells the producer to
/// stop. A file's head must declare exactly the file's length.
fn parts_of(delivery: Delivery, head_only: bool) -> Result<(u16, Vec<Header>, Reply), String> {
    let nothing = || Reply::Whole(Full::new(Bytes::new()));
    Ok(match delivery {
        Delivery::Whole(answer) => {
            let body = if head_only { Bytes::new() } else { answer.body };
            (answer.status, answer.headers, Reply::Whole(Full::new(body)))
        }
        Delivery::Streamed(Streamed {
            status,
            headers,
            chunks,
            pace,
        }) => {
            let body = if head_only {
                nothing()
            } else {
                Reply::Live(Live::with_length(chunks, declared_length(&headers)?).paced(pace))
            };
            (status, headers, body)
        }
        Delivery::File(FileAnswer {
            status,
            headers,
            file,
        }) => {
            if declared_length(&headers)? != Some(file.len()) {
                return Err("the file's content-length does not match its size".to_owned());
            }
            (
                status,
                headers,
                if head_only {
                    nothing()
                } else {
                    Reply::File(file)
                },
            )
        }
        Delivery::Upgrade(_) => return Err("an upgrade is not a plain answer".to_owned()),
    })
}

/// [`parts_of`], then the compression policy, when there is one, applied to the
/// head and body.
fn compressed_parts(
    delivery: Delivery,
    head_only: bool,
    offer: Option<&Offer>,
) -> Result<(u16, Vec<Header>, Reply), String> {
    let shape = Shape::of(&delivery);
    let (status, mut headers, body) = parts_of(delivery, head_only)?;
    let body = match offer {
        Some(offer) => offer.apply(status, &mut headers, body, shape, head_only),
        None => body,
    };
    Ok((status, headers, body))
}

/// Build the hyper response, or say why the answer cannot be sent as given (a
/// status or header hyper rejects, an unreadable `content-length`); the caller
/// then answers a bare 500. So does an informational status: `101` would hand
/// the connection to hyper's upgrade path (and h2 has no 101), and the server
/// serves no protocol that switches. A streamed body is held to the
/// `content-length` its head declares.
fn response_of(
    delivery: Delivery,
    head_only: bool,
    offer: Option<&Offer>,
) -> Result<Response<Reply>, String> {
    let (status, headers, body) = compressed_parts(delivery, head_only, offer)?;
    let status = StatusCode::from_u16(status)
        .ok()
        .filter(|status| !status.is_informational())
        .ok_or_else(|| format!("status {status} cannot be sent"))?;
    let mut response = Response::new(body);
    *response.status_mut() = status;
    for (name, value) in &headers {
        let invalid = || format!("invalid header {:?}", String::from_utf8_lossy(name));
        let name = HeaderName::from_bytes(name).map_err(|_| invalid())?;
        let value = HeaderValue::from_bytes(value).map_err(|_| invalid())?;
        response.headers_mut().append(name, value);
    }
    Ok(response)
}

/// The `content-length` a head declares: `None` without one, an error when
/// it is not a number or repeated with different values.
pub(crate) fn declared_length(headers: &[Header]) -> Result<Option<u64>, String> {
    let mut declared = None;
    for (_, value) in headers
        .iter()
        .filter(|(name, _)| name.eq_ignore_ascii_case(b"content-length"))
    {
        let length = std::str::from_utf8(value)
            .ok()
            .filter(|text| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|text| text.parse::<u64>().ok())
            .ok_or_else(|| "invalid content-length".to_owned())?;
        if declared.is_some_and(|seen| seen != length) {
            return Err("conflicting content-length headers".to_owned());
        }
        declared = Some(length);
    }
    Ok(declared)
}

/// A switched connection waiting for its `101` to be written: once hyper
/// has, [`Switched::on`] resolves with the raw stream.
#[derive(Debug)]
pub struct Switched {
    pub on: OnUpgrade,
    pub session: Session,
}

/// Where [`respond`] leaves a [`Switched`] for the connection that owns it.
pub type Handover = Arc<Mutex<Option<Switched>>>;

/// The `101` that accepts `handshake`, and the handover of the session; why
/// not, when the handshake was not an offer or the backend chose a subprotocol
/// the client did not offer.
fn switch(
    accepted: Accepted,
    handshake: &Upgrade,
    on: OnUpgrade,
    transport: &Transport,
) -> Result<Response<Reply>, String> {
    let Upgrade::Offer(offer) = handshake else {
        return Err("there is no WebSocket handshake to accept".to_owned());
    };
    let Accepted {
        subprotocol,
        headers,
        session,
    } = accepted;
    let mut response = Response::new(Reply::Whole(Full::new(Bytes::new())));
    *response.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let fields = response.headers_mut();
    fields.insert("upgrade", HeaderValue::from_static("websocket"));
    fields.insert("connection", HeaderValue::from_static("Upgrade"));
    let key = HeaderValue::from_str(&ws::accept_key(offer.key.as_bytes()))
        .map_err(|_| "invalid accept key".to_owned())?;
    fields.insert("sec-websocket-accept", key);
    if let Some(chosen) = subprotocol {
        if !offer.protocols.contains(&chosen) {
            return Err(format!("subprotocol {chosen:?} was not offered"));
        }
        let value = HeaderValue::from_str(&chosen).map_err(|_| "invalid subprotocol".to_owned())?;
        fields.insert("sec-websocket-protocol", value);
    }
    for (name, value) in &headers {
        let invalid = || format!("invalid header {:?}", String::from_utf8_lossy(name));
        let name = HeaderName::from_bytes(name).map_err(|_| invalid())?;
        let value = HeaderValue::from_bytes(value).map_err(|_| invalid())?;
        fields.append(name, value);
    }
    transport.signals.upgraded.store(true, Ordering::Release);
    *transport
        .handover
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(Switched { on, session });
    Ok(response)
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
    /// Add a `Server-Timing` header with the time taken and the serving lane.
    pub server_timing: bool,
    /// Compress answers by this policy; `None` sends them as they are.
    pub compression: Option<Arc<Compression>>,
    /// Record every answer here; `None` records nothing.
    pub metrics: Option<Arc<Registry>>,
    /// Where an accepted WebSocket upgrade is left for the connection.
    pub handover: Handover,
}

/// What `respond` keeps of a request for the metrics, past the handoff.
struct Metered {
    registry: Arc<Registry>,
    trace: Arc<Trace>,
    method: usize,
    in_flight: InFlightGuard,
}

impl Metered {
    fn begin(registry: &Arc<Registry>, incoming: &Incoming) -> Self {
        Self {
            registry: Arc::clone(registry),
            trace: Arc::clone(&incoming.trace),
            method: Registry::method_index(&incoming.method),
            in_flight: registry.enter(),
        }
    }

    /// Record the answer and count the bytes of its body as they go out.
    fn record(
        self,
        response: Response<Reply>,
        elapsed: Duration,
        invalid: bool,
    ) -> Response<Reply> {
        let slot = self.registry.slot_of(self.trace.action());
        self.registry
            .observe(slot, self.method, response.status().as_u16(), elapsed);
        if invalid {
            self.registry.error(slot, ErrorKind::InvalidResponse);
        }
        drop(self.in_flight);
        response.map(|body| Counted::wrap(body, &self.registry, slot))
    }
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

/// `kiso;dur=3.2;desc="native"`: what browser developer tools show next to the
/// request.
fn server_timing(elapsed: Duration, lane: Option<Lane>) -> HeaderValue {
    let millis = elapsed.as_secs_f64() * 1_000.0;
    let text = match lane {
        Some(lane) => format!("kiso;dur={millis:.3};desc=\"{}\"", lane.name()),
        None => format!("kiso;dur={millis:.3}"),
    };
    HeaderValue::from_str(&text).unwrap_or_else(|_| HeaderValue::from_static("kiso"))
}

/// Serve one request: ask the backend, write the answer. The connection closes
/// after an answer given while the body was not read in full (unread, refused,
/// cut short) and when the framing guard asks for it: in each case the bytes that follow must not be parsed as a request.
pub async fn respond<B: Backend>(
    backend: Arc<B>,
    peer: SocketAddr,
    transport: Transport,
    mut request: Request<RequestBody>,
) -> Result<Response<Reply>, Infallible> {
    let on_upgrade = hyper::upgrade::on(&mut request);
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
    let timed = transport.server_timing.then(|| Arc::clone(&incoming.trace));
    let metered = transport
        .metrics
        .as_ref()
        .map(|registry| Metered::begin(registry, &incoming));
    let offer = transport
        .compression
        .as_ref()
        .map(|policy| policy.offer(&incoming.headers));
    let handshake = incoming.upgrade.clone();
    let delivery = backend.deliver(incoming).await;
    let mut invalid = false;
    let built = match delivery {
        Delivery::Upgrade(accepted) => switch(*accepted, &handshake, on_upgrade, &transport),
        other => response_of(other, head_only, offer.as_ref()),
    };
    let mut response = built.unwrap_or_else(|reason| {
        invalid = true;
        stream::report(&format!(
            "the response cannot be sent ({reason}); answering 500 instead"
        ));
        plain(StatusCode::INTERNAL_SERVER_ERROR)
    });
    if let Some(metered) = metered {
        response = metered.record(response, started.elapsed(), invalid);
    }
    let status = response.status().as_u16();
    let timing = timed.map(|trace| server_timing(started.elapsed(), trace.lane()));
    if let Some((style, head)) = logged {
        let elapsed = started.elapsed();
        if transport.log_filter.keeps(&head.path, status, elapsed) {
            head.log(style, status, elapsed, transport.log_filter.sink.as_ref());
        }
    }
    if let Some(value) = timing {
        response.headers_mut().insert("server-timing", value);
    }
    let unread = finished.is_some_and(|done| !done.load(Ordering::Acquire));
    if unread {
        transport.signals.linger.store(true, Ordering::Release);
    }
    // `connection` is a forbidden header on HTTP/2, which has no such notion.
    let closing = unread || transport.signals.close.load(Ordering::Acquire);
    if closing && version != Version::HTTP_2 && status != 101 {
        response
            .headers_mut()
            .insert("connection", HeaderValue::from_static("close"));
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn whole(status: u16, headers: Vec<Header>, body: &[u8]) -> Delivery {
        Delivery::Whole(Answer {
            status,
            headers,
            body: Bytes::copy_from_slice(body),
        })
    }

    #[test]
    fn server_timing_names_the_time_and_the_lane() {
        let took = Duration::from_micros(3_250);
        let value = server_timing(took, Some(Lane::Native));
        assert_eq!(value, "kiso;dur=3.250;desc=\"native\"");
        assert_eq!(server_timing(took, None), "kiso;dur=3.250");
    }

    #[test]
    fn head_keeps_the_declared_length_and_drops_the_body() {
        let headers = vec![(b"content-length".to_vec(), b"5".to_vec())];
        let response = response_of(whole(200, headers, b"hello"), true, None).unwrap();
        assert_eq!(response.headers()["content-length"], "5");
        assert!(response.body().is_end_stream());
    }

    #[test]
    fn repeated_headers_stay_separate() {
        let headers = vec![
            (b"set-cookie".to_vec(), b"a=1".to_vec()),
            (b"set-cookie".to_vec(), b"b=2".to_vec()),
        ];
        let response = response_of(whole(200, headers, b""), false, None).unwrap();
        assert_eq!(response.headers().get_all("set-cookie").iter().count(), 2);
    }

    #[test]
    fn unrepresentable_answers_are_refused_with_a_reason() {
        let reason = |delivery| match response_of(delivery, false, None) {
            Err(reason) => reason,
            Ok(_) => panic!("the answer was accepted"),
        };
        assert_eq!(reason(whole(99, vec![], b"")), "status 99 cannot be sent");
        assert_eq!(reason(whole(101, vec![], b"")), "status 101 cannot be sent");
        let bad_name = vec![(b"bad name".to_vec(), b"x".to_vec())];
        assert!(reason(whole(200, bad_name, b"")).contains("bad name"));
        let bad_value = vec![(b"x".to_vec(), b"a\nb".to_vec())];
        assert_eq!(reason(whole(200, bad_value, b"")), "invalid header \"x\"");
    }

    #[test]
    fn a_streamed_content_length_must_be_one_number() {
        let length = |values: &[&[u8]]| {
            let headers: Vec<Header> = values
                .iter()
                .map(|value| (b"Content-Length".to_vec(), value.to_vec()))
                .collect();
            declared_length(&headers)
        };
        assert_eq!(length(&[]), Ok(None));
        assert_eq!(length(&[b"5", b"5"]), Ok(Some(5)));
        assert!(length(&[b"5", b"6"]).is_err());
        for bad in [&b""[..], b"-1", b"+5", b"5 ", b"x"] {
            assert!(length(&[bad]).is_err(), "{bad:?}");
        }
    }
}
