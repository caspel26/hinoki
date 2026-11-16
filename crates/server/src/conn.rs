//! One HTTP/1.1 request on one connection: what the transport hands to the
//! [`Backend`] and how the [`Answer`] is written back.
//!
//! This layer owns only transport rules: HEAD answers keep their
//! `content-length` and send no body, the connection closes after a request that
//! carries a body (this server does not read bodies yet), and a request the
//! protocol forbids (HTTP/1.1 without exactly one `Host`) is flagged in
//! [`Incoming::refusal`], so the backend answers it like any other early
//! answer. Everything that decides an answer belongs to the backend.

use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Incoming as RequestBody};
use hyper::header::{HeaderName, HeaderValue};
use hyper::{Method, Request, Response, StatusCode, Version};

use crate::early::Early;
use crate::headers::Header;

/// The head of a request as the backend sees it. Header names are lowercase,
/// repeated headers stay separate and in arrival order, and `path` and `query`
/// are the raw (still percent-encoded) target. An absolute-form target makes its
/// authority the `host` header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Incoming {
    pub method: String,
    pub path: String,
    pub query: Vec<u8>,
    pub headers: Vec<Header>,
    pub peer: SocketAddr,
    /// Set when the transport rejects the request ([`Early::BadHost`] or
    /// [`Early::NotImplemented`]): the backend answers with it and does not
    /// route the request.
    pub refusal: Option<Early>,
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

/// The transport's verdict on a request. A body is refused first; then HTTP/1.1
/// needs exactly one `Host` (RFC 9112 3.2). An absolute-form target (RFC 9112
/// 3.2.2) makes its authority the host, replacing the header.
fn refusal_of(request: &Request<RequestBody>, headers: &mut Vec<Header>) -> Option<Early> {
    let hosts = headers.iter().filter(|(name, _)| name == b"host").count();
    if let Some(authority) = request.uri().authority() {
        headers.retain(|(name, _)| name != b"host");
        headers.push((b"host".to_vec(), authority.as_str().as_bytes().to_vec()));
    }
    if !request.body().is_end_stream() {
        return Some(Early::NotImplemented);
    }
    (request.version() == Version::HTTP_11 && hosts != 1).then_some(Early::BadHost)
}

fn incoming_of(request: &Request<RequestBody>, peer: SocketAddr) -> Incoming {
    let uri = request.uri();
    let mut headers: Vec<Header> = request
        .headers()
        .iter()
        .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
        .collect();
    let refusal = refusal_of(request, &mut headers);
    Incoming {
        method: request.method().as_str().to_owned(),
        path: uri.path().to_owned(),
        query: uri.query().unwrap_or("").as_bytes().to_vec(),
        headers,
        peer,
        refusal,
    }
}

/// Build the hyper response. An answer the transport cannot represent (a status
/// or header hyper rejects) becomes a bare 500 rather than a broken stream.
fn response_of(answer: Answer, head_only: bool) -> Response<Full<Bytes>> {
    let Ok(status) = StatusCode::from_u16(answer.status) else {
        return plain(StatusCode::INTERNAL_SERVER_ERROR);
    };
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

/// Serve one request: ask the backend, write the answer. A request with a body
/// closes the connection, since the unread body would otherwise be parsed as the
/// next request.
pub async fn respond<B: Backend>(
    backend: Arc<B>,
    peer: SocketAddr,
    request: Request<RequestBody>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    let head_only = request.method() == Method::HEAD;
    let incoming = incoming_of(&request, peer);
    let close = incoming.refusal == Some(Early::NotImplemented);
    let mut response = response_of(backend.handle(incoming).await, head_only);
    if close {
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
        let bad_name = vec![(b"bad name".to_vec(), b"x".to_vec())];
        assert_eq!(response_of(answer(200, bad_name, b""), false).status(), 500);
        let bad_value = vec![(b"x".to_vec(), b"a\nb".to_vec())];
        assert_eq!(
            response_of(answer(200, bad_value, b""), false).status(),
            500
        );
    }
}
