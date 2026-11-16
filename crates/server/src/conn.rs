//! One HTTP/1.1 request on one connection: what the transport hands to the
//! [`Backend`] and how the [`Answer`] is written back.
//!
//! This layer owns only transport rules: HEAD answers keep their
//! `content-length` and send no body, and a request that carries a body is
//! refused (this server does not read bodies yet). Everything that decides an
//! answer belongs to the backend.

use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Incoming as RequestBody};
use hyper::header::{HeaderName, HeaderValue};
use hyper::{Method, Request, Response, StatusCode};

use crate::headers::Header;

/// The head of a request as the backend sees it. Header names are lowercase,
/// repeated headers stay separate and in arrival order, and `path` and `query`
/// are the raw (still percent-encoded) target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Incoming {
    pub method: String,
    pub path: String,
    pub query: Vec<u8>,
    pub headers: Vec<Header>,
    pub peer: SocketAddr,
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

const NO_BODY_SUPPORT: &[u8] = b"{\"error\": \"not_implemented\"}";

fn plain(status: StatusCode) -> Response<Full<Bytes>> {
    let mut response = Response::new(Full::new(Bytes::new()));
    *response.status_mut() = status;
    response
}

/// Refuse a request with a body and close the connection, since the unread
/// body would otherwise be parsed as the next request.
fn refuse_body() -> Response<Full<Bytes>> {
    let mut response = Response::new(Full::new(Bytes::from_static(NO_BODY_SUPPORT)));
    *response.status_mut() = StatusCode::NOT_IMPLEMENTED;
    let headers = response.headers_mut();
    headers.insert("content-type", HeaderValue::from_static("application/json"));
    headers.insert("connection", HeaderValue::from_static("close"));
    headers.insert("content-length", HeaderValue::from(NO_BODY_SUPPORT.len()));
    response
}

fn incoming_of(request: &Request<RequestBody>, peer: SocketAddr) -> Incoming {
    let uri = request.uri();
    Incoming {
        method: request.method().as_str().to_owned(),
        path: uri.path().to_owned(),
        query: uri.query().unwrap_or("").as_bytes().to_vec(),
        headers: request
            .headers()
            .iter()
            .map(|(name, value)| (name.as_str().as_bytes().to_vec(), value.as_bytes().to_vec()))
            .collect(),
        peer,
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

/// Serve one request: refuse a body, ask the backend, write the answer.
pub async fn respond<B: Backend>(
    backend: Arc<B>,
    peer: SocketAddr,
    request: Request<RequestBody>,
) -> Result<Response<Full<Bytes>>, Infallible> {
    if !request.body().is_end_stream() {
        return Ok(refuse_body());
    }
    let head_only = request.method() == Method::HEAD;
    let answer = backend.handle(incoming_of(&request, peer)).await;
    Ok(response_of(answer, head_only))
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
